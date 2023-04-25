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

from dataclasses import dataclass
from pathlib import Path

import librosa
import torch.utils.data
import traceback
from typing import Dict, List, Optional, Tuple

from nemo.collections.tts.parts.preprocessing.feature_processors import FeatureProcessor
from nemo.collections.asr.parts.preprocessing.segment import AudioSegment
from nemo.collections.asr.parts.utils.manifest_utils import read_manifest
from nemo.collections.tts.parts.utils.tts_dataset_utils import (
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
    sample_weight: float = 1.0


@dataclass
class DatasetSample:
    manifest_entry: dict
    audio_dir: Path


@experimental
class VocoderDataset(Dataset):

    def __init__(
        self,
        dataset_meta: Dict[str, DatasetMeta],
        sample_rate: int,
        n_segments: Optional[int] = None,
        weighted_sample_steps: Optional[int] = None,
        feature_processors: Optional[Dict[str, FeatureProcessor]] = None,
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
    ):
        super().__init__()

        self.sample_rate = sample_rate
        self.n_segments = n_segments
        self.weighted_sample_steps = weighted_sample_steps
        self.load_precomputed_mel = False

        if feature_processors:
            logging.info(f"Found feature processors {feature_processors.keys()}")
            self.feature_processors = feature_processors.values()
        else:
            self.feature_processors = []

        self.data_samples = []
        self.sample_weights = []
        for dataset_name, dataset in dataset_meta.items():
            samples, weights = self._process_dataset(
                dataset_name=dataset_name,
                dataset=dataset,
                min_duration=min_duration,
                max_duration=max_duration,
            )
            self.data_samples += samples
            self.sample_weights += weights

    def get_sampler(self, batch_size: int) -> Optional[torch.utils.data.Sampler]:
        if not self.weighted_sample_steps:
            return None

        sampler = get_weighted_sampler(
            sample_weights=self.sample_weights,
            batch_size=batch_size,
            num_steps=self.weighted_sample_steps
        )
        return sampler

    def _segment_audio(self, audio_filepath: Path) -> AudioSegment:
        # Retry file read multiple times as file seeking can produce random IO errors.
        for _ in range(3):
            try:
                audio_segment = AudioSegment.segment_from_file(
                    audio_filepath,
                    target_sr=self.sample_rate,
                    n_segments=self.n_segments,
                )
                return audio_segment
            except Exception:
                traceback.print_exc()

        raise ValueError(f"Failed to read audio {audio_filepath}")

    def _sample_audio(self, audio_filepath: Path) -> Tuple[torch.Tensor, torch.Tensor]:
        if not self.n_segments:
            audio_array, _ = librosa.load(audio_filepath, sr=self.sample_rate)
        else:
            audio_segment = self._segment_audio(audio_filepath)
            audio_array = audio_segment.samples
        audio = torch.tensor(audio_array)
        audio_len = torch.tensor(audio.shape[0])
        return audio, audio_len

    @staticmethod
    def _process_dataset(
        dataset_name: str,
        dataset: DatasetMeta,
        min_duration: float,
        max_duration: float,
    ):
        entries = read_manifest(dataset.manifest_path)
        filtered_entries, total_hours, filtered_hours = filter_dataset_by_duration(
            entries=entries,
            min_duration=min_duration,
            max_duration=max_duration
        )

        logging.info(dataset_name)
        logging.info(f"Original # of files: {len(entries)}")
        logging.info(f"Filtered # of files: {len(filtered_entries)}")
        logging.info(f"Original duration: {total_hours} hours")
        logging.info(f"Filtered duration: {filtered_hours} hours")

        samples = []
        sample_weights = []
        for entry in filtered_entries:
            sample = DatasetSample(
                manifest_entry=entry,
                audio_dir=Path(dataset.audio_dir),
            )
            samples.append(sample)
            sample_weights.append(dataset.sample_weight)

        return samples, sample_weights

    def __len__(self):
        return len(self.data_samples)

    def __getitem__(self, index):
        data = self.data_samples[index]

        audio_filepath = Path(data.manifest_entry["audio_filepath"])
        audio_filepath_abs, audio_filepath_rel = get_abs_rel_paths(input_path=audio_filepath, base_path=data.audio_dir)

        audio, audio_len = self._sample_audio(audio_filepath_abs)

        example = {
            "audio_filepath": audio_filepath_rel,
            "audio": audio,
            "audio_len": audio_len
        }

        for processor in self.feature_processors:
            processor.process(example)

        return example

    def collate_fn(self, batch: List[dict]):
        audio_filepath_list = []
        audio_list = []
        audio_len_list = []

        for example in batch:
            audio_filepath_list.append(example["audio_filepath"])

            audio_tensor = torch.tensor(example["audio"], dtype=torch.float32)
            audio_list.append(audio_tensor)
            audio_len_list.append(audio_tensor.shape[0])

        batch_audio_len = torch.IntTensor(audio_len_list)
        audio_max_len = int(batch_audio_len.max().item())

        batch_audio = stack_tensors(audio_list, max_lens=[audio_max_len])

        batch_dict = {
            "audio_filepaths": audio_filepath_list,
            "audio": batch_audio,
            "audio_lens": batch_audio_len,
        }

        return batch_dict
