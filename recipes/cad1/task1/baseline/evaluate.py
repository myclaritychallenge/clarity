import csv
import hashlib
import itertools
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import hydra
import numpy as np
import pandas as pd
from omegaconf import DictConfig
from scipy.io import wavfile

from clarity.evaluator.haaqi import haaqi_v1

logger = logging.getLogger(__name__)


class ResultsFile:
    """A utility class for writing results to a CSV file.

    Attributes:
        file_name (str): The name of the file to write results to.
    """

    def __init__(self, file_name: str):
        """Initialize the ResultsFile instance.

        Args:
            file_name (str): The name of the file to write results to.
        """
        self.file_name = file_name

    def write_header(self):
        """Write the header row to the CSV file."""
        with open(self.file_name, "w", encoding="utf-8", newline="") as csv_file:
            csv_writer = csv.writer(
                csv_file, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL
            )
            csv_writer.writerow(
                [
                    "song",
                    "listener",
                    "score",
                    "left_bass",
                    "right_bass",
                    "left_drums",
                    "right_drums",
                    "left_other",
                    "right_other",
                    "left_vocals",
                    "right_vocals",
                ]
            )

    def add_result(
        self,
        listener: str,
        song: str,
        score: float,
        instruments_scores: Dict[str, float],
    ):
        """Add a result to the CSV file.

        Args:
            listener (str): The name of the listener who submitted the result.
            song (str): The name of the song that the result is for.
            score (float): The combined score for the result.
            instruments_scores (dict): A dictionary of scores for each instrument channel in the result.
        """
        logger.info(f"The combined score is {score}")

        with open(self.file_name, "a", encoding="utf-8", newline="") as csv_file:
            csv_writer = csv.writer(
                csv_file, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL
            )
            csv_writer.writerow(
                [
                    song,
                    listener,
                    str(score),
                    str(instruments_scores["left_bass"]),
                    str(instruments_scores["right_bass"]),
                    str(instruments_scores["left_drums"]),
                    str(instruments_scores["right_drums"]),
                    str(instruments_scores["left_other"]),
                    str(instruments_scores["right_other"]),
                    str(instruments_scores["left_vocals"]),
                    str(instruments_scores["right_vocals"]),
                ]
            )


def compute_haaqi(
    enh_signal: np.ndarray,
    ref_signal: np.ndarray,
    audiogram: np.ndarray,
    audiogram_frequencies: np.ndarray,
    fs_signal: int,
) -> float:
    """Compute HAAQI metric"""

    haaqi_audiogram_freq = [250, 500, 1000, 2000, 4000, 6000]
    audiogram_adjusted = np.array(
        [
            audiogram[i]
            for i in range(len(audiogram_frequencies))
            if audiogram_frequencies[i] in haaqi_audiogram_freq
        ]
    )
    score, _, _, _ = haaqi_v1(
        reference=ref_signal,
        reference_freq=fs_signal,
        processed=enh_signal,
        processed_freq=fs_signal,
        hearing_loss=audiogram_adjusted,
        equalisation=1,
    )
    return score


def set_song_seed(song: str) -> None:
    """Set a seed that is unique for the given song"""
    song_encoded = hashlib.md5(song.encode("utf-8")).hexdigest()
    song_md5 = int(song_encoded, 16) % (10**8)
    np.random.seed(song_md5)


def make_song_listener_list(
    songs: List[str], listeners: Dict[str, Any], small_test: bool = False
) -> List[Tuple[str, str]]:
    """Make the list of scene-listener pairing to process"""
    song_listener_pairs = list(itertools.product(songs, listeners.keys()))

    if small_test:
        song_listener_pairs = song_listener_pairs[::15]

    return song_listener_pairs


@hydra.main(config_path="", config_name="config")
def run_calculate_aq(cfg: DictConfig) -> None:
    """Evaluate the enhanced signals using the HAAQI metric."""
    # Load test songs
    with open(cfg.path.valid_file, "r", encoding="utf-8") as fp:
        songs = json.load(fp)
    songs = pd.DataFrame.from_dict(songs)

    # Load listener data
    with open(cfg.path.listeners_file, "r", encoding="utf-8") as fp:
        listener_audiograms = json.load(fp)

    enhanced_folder = Path("enhanced_signals")
    logger.info(f"Evaluating from {enhanced_folder} directory")

    results_file = ResultsFile("scores.csv")
    results_file.write_header()

    song_listener_pair = make_song_listener_list(
        songs["Track Name"].tolist(), listener_audiograms, cfg.evaluate.small_test
    )

    for song, listener in song_listener_pair:
        logger.info(f"Evaluating {song} for {listener}")

        if cfg.evaluate.set_random_seed:
            set_song_seed(song)

        scores = {}
        for instrument in [
            "drums",
            "bass",
            "other",
            "vocals",
        ]:
            logger.info(f"...evaluating {instrument}")
            split_dir = "train"
            if songs[songs["Track Name"] == song]["Split"].tolist()[0] == "test":
                split_dir = "test"

            sample_rate_reference_signal, reference_signal = wavfile.read(
                Path(cfg.path.music_dir) / split_dir / song / f"{instrument}.wav"
            )

            reference_signal = (reference_signal / 32768.0).astype(np.float32)
            left_reference_signal = reference_signal[:, 0]
            right_reference_signal = reference_signal[:, 1]

            sample_rate_left_enhanced_signal, left_enhanced_signal = wavfile.read(
                enhanced_folder / f"{listener}_{song}_left_{instrument}.wav"
            )
            sample_rate_right_enhanced_signal, right_enhanced_signal = wavfile.read(
                enhanced_folder / f"{listener}_{song}_right_{instrument}.wav"
            )

            assert (
                sample_rate_reference_signal
                == sample_rate_left_enhanced_signal
                == sample_rate_right_enhanced_signal
                == cfg.nalr.fs
            )

            #  audiogram, audiogram_frequencies, fs_signal
            scores[f"left_{instrument}"] = compute_haaqi(
                left_enhanced_signal,
                left_reference_signal,
                np.array(listener_audiograms[listener]["audiogram_levels_l"]),
                np.array(listener_audiograms[listener]["audiogram_cfs"]),
                cfg.nalr.fs,
            )
            scores[f"right_{instrument}"] = compute_haaqi(
                right_enhanced_signal,
                right_reference_signal,
                np.array(listener_audiograms[listener]["audiogram_levels_r"]),
                np.array(listener_audiograms[listener]["audiogram_cfs"]),
                cfg.nalr.fs,
            )

        # Compute the combined score
        combined_score = np.mean(list(scores.values()))
        results_file.add_result(
            listener,
            song,
            score=float(combined_score),
            instruments_scores=scores,
        )


if __name__ == "__main__":
    run_calculate_aq()
