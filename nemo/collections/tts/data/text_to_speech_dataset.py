# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import librosa
import torch.utils.data

from nemo.collections.asr.parts.utils.manifest_utils import read_manifest
from nemo.collections.common.tokenizers.text_to_speech.tts_tokenizers import BaseTokenizer
from nemo.collections.tts.parts.preprocessing.feature_processors import FeatureProcessor
from nemo.collections.tts.parts.preprocessing.features import Featurizer
from nemo.collections.tts.parts.utils.tts_dataset_utils import (
    BetaBinomialInterpolator,
    beta_binomial_prior_distribution,
    filter_dataset_by_duration,
    get_abs_rel_paths,
    get_weighted_sampler,
    stack_tensors,
)
from nemo.core.classes import Dataset
from nemo.utils import logging
from nemo.utils.decorators import experimental


@dataclass
class DatasetMeta:
    manifest_path: Path
    audio_dir: Path
    feature_dir: Path
    sample_weight: float = 1.0


@dataclass
class DatasetSample:
    manifest_entry: Dict[str, Any]
    audio_dir: Path
    feature_dir: Path
    text: str
    speaker: str
    speaker_index: int = None


@dataclass
class AlignPriorConfig:
    hop_length: int
    use_beta_binomial_interpolator: bool = False


@experimental
class TextToSpeechDataset(Dataset):
    """
    Class for processing and loading text to speech training examples.

    Args:
        dataset_meta: Dict of dataset names (string) to dataset metadata.
        sample_rate: Sample rate to load audio as. If the audio is stored at a different sample rate, then it will
            be resampled.
        text_tokenizer: Tokenizer to apply to the text field.
        weighted_sample_steps: Optional int, If provided, then data will be sampled (with replacement) based on
            the sample weights provided in the dataset metadata. If None, then sample weights will be ignored.
        speaker_path: Optional, path to JSON file with speaker indices, for multi-speaker training. Can be created with
            scripts.dataset_processing.tts.create_speaker_map.py
        featurizers: Optional, list of featurizers to load feature data from. Should be the same config provided
            when running scripts.dataset_processing.tts.compute_features.py before training.
        feature_processors: Optional, list of feature processors to run on training examples.
        align_prior_config: Optional, if provided alignment prior will be calculated and included in
            batch output.
        min_duration: Optional float, if provided audio files in the training manifest shorter than 'min_duration'
            will be ignored.
        max_duration: Optional float, if provided audio files in the training manifest longer than 'max_duration'
            will be ignored.
    """

    def __init__(
        self,
        dataset_meta: Dict[str, DatasetMeta],
        sample_rate: int,
        text_tokenizer: BaseTokenizer,
        weighted_sample_steps: Optional[int] = None,
        speaker_path: Optional[Path] = None,
        featurizers: Optional[Dict[str, Featurizer]] = None,
        feature_processors: Optional[Dict[str, FeatureProcessor]] = None,
        align_prior_config: Optional[AlignPriorConfig] = None,
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
    ):
        super().__init__()

        self.sample_rate = sample_rate
        self.text_tokenizer = text_tokenizer
        self.weighted_sample_steps = weighted_sample_steps

        if speaker_path:
            self.include_speaker = True
            with open(speaker_path, 'r', encoding="utf-8") as speaker_f:
                speaker_index_map = json.load(speaker_f)
        else:
            self.include_speaker = False
            speaker_index_map = None

        if featurizers:
            logging.info(f"Found featurizers {featurizers.keys()}")
            self.featurizers = featurizers.values()
        else:
            self.featurizers = []

        if feature_processors:
            logging.info(f"Found featurize processors {feature_processors.keys()}")
            self.feature_processors = feature_processors.values()
        else:
            self.feature_processors = []

        self.align_prior_config = align_prior_config
        if self.align_prior_config.use_beta_binomial_interpolator:
            self.beta_binomial_interpolator = BetaBinomialInterpolator()
        else:
            self.beta_binomial_interpolator = None

        self.data_samples = []
        self.sample_weights = []
        for dataset_name, dataset in dataset_meta.items():
            samples, weights = self._process_dataset(
                dataset_name=dataset_name,
                dataset=dataset,
                min_duration=min_duration,
                max_duration=max_duration,
                speaker_index_map=speaker_index_map,
            )
            self.data_samples += samples
            self.sample_weights += weights

    def get_sampler(self, batch_size: int) -> Optional[torch.utils.data.Sampler]:
        if not self.weighted_sample_steps:
            return None

        sampler = get_weighted_sampler(
            sample_weights=self.sample_weights, batch_size=batch_size, num_steps=self.weighted_sample_steps
        )
        return sampler

    def _process_dataset(
        self,
        dataset_name: str,
        dataset: DatasetMeta,
        min_duration: float,
        max_duration: float,
        speaker_index_map: Dict[str, int],
    ):
        entries = read_manifest(dataset.manifest_path)
        filtered_entries, total_hours, filtered_hours = filter_dataset_by_duration(
            entries=entries, min_duration=min_duration, max_duration=max_duration
        )

        logging.info(dataset_name)
        logging.info(f"Original # of files: {len(entries)}")
        logging.info(f"Filtered # of files: {len(filtered_entries)}")
        logging.info(f"Original duration: {total_hours} hours")
        logging.info(f"Filtered duration: {filtered_hours} hours")

        samples = []
        sample_weights = []
        for entry in filtered_entries:

            if "normalized_text" in entry:
                text = entry["normalized_text"]
            else:
                text = entry["text"]

            if self.include_speaker:
                speaker = entry["speaker"]
                speaker_index = speaker_index_map[speaker]
            else:
                speaker = None
                speaker_index = 0

            sample = DatasetSample(
                manifest_entry=entry,
                audio_dir=dataset.audio_dir,
                feature_dir=dataset.feature_dir,
                text=text,
                speaker=speaker,
                speaker_index=speaker_index,
            )
            samples.append(sample)
            sample_weights.append(dataset.sample_weight)

        return samples, sample_weights

    def __len__(self):
        return len(self.data_samples)

    def __getitem__(self, index):
        data = self.data_samples[index]

        audio_filepath = Path(data.manifest_entry["audio_filepath"])
        audio_path, _ = get_abs_rel_paths(input_path=audio_filepath, base_path=data.audio_dir)

        audio, _ = librosa.load(audio_path, sr=self.sample_rate)
        tokens = self.text_tokenizer(data.text)

        example = {"audio": audio, "tokens": tokens}

        if data.speaker is not None:
            example["speaker"] = data.speaker
            example["speaker_index"] = data.speaker_index

        if self.align_prior_config:
            text_len = len(tokens)
            spec_len = 1 + librosa.core.samples_to_frames(
                audio.shape[0], hop_length=self.align_prior_config.hop_length
            )
            if self.beta_binomial_interpolator:
                align_prior = self.beta_binomial_interpolator(w=spec_len, h=text_len)
            else:
                align_prior = beta_binomial_prior_distribution(phoneme_count=text_len, mel_count=spec_len)
            align_prior = torch.tensor(align_prior, dtype=torch.float32)
            example["align_prior"] = align_prior

        for featurizer in self.featurizers:
            feature_dict = featurizer.load(
                manifest_entry=data.manifest_entry, audio_dir=data.audio_dir, feature_dir=data.feature_dir
            )
            example.update(feature_dict)

        for processor in self.feature_processors:
            processor.process(example)

        return example

    def collate_fn(self, batch: List[dict]):

        audio_list = []
        audio_len_list = []
        token_list = []
        token_len_list = []
        speaker_list = []
        prior_list = []

        for example in batch:
            audio_tensor = torch.tensor(example["audio"], dtype=torch.float32)
            audio_list.append(audio_tensor)
            audio_len_list.append(audio_tensor.shape[0])

            token_tensor = torch.tensor(example["tokens"], dtype=torch.int32)
            token_list.append(token_tensor)
            token_len_list.append(token_tensor.shape[0])

            if self.include_speaker:
                speaker_list.append(example["speaker_index"])

            if self.align_prior_config:
                prior_list.append(example["align_prior"])

        batch_audio_len = torch.IntTensor(audio_len_list)
        audio_max_len = int(batch_audio_len.max().item())

        batch_token_len = torch.IntTensor(token_len_list)
        token_max_len = int(batch_token_len.max().item())

        batch_audio = stack_tensors(audio_list, max_lens=[audio_max_len])
        batch_tokens = stack_tensors(token_list, max_lens=[token_max_len], pad_value=self.text_tokenizer.pad)

        batch_dict = {
            "audio": batch_audio,
            "audio_lens": batch_audio_len,
            "text": batch_tokens,
            "text_lens": batch_token_len,
        }

        if self.include_speaker:
            batch_dict["speaker_id"] = torch.IntTensor(speaker_list)

        if self.align_prior_config:
            spec_max_len = max([prior.shape[0] for prior in prior_list])
            text_max_len = max([prior.shape[1] for prior in prior_list])
            batch_dict["align_prior_matrix"] = stack_tensors(prior_list, max_lens=[text_max_len, spec_max_len],)

        for featurizer in self.featurizers:
            feature_dict = featurizer.collate_fn(batch)
            batch_dict.update(feature_dict)

        return batch_dict
