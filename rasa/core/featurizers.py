import io
import jsonpickle
import logging
import numpy as np
import os
from tqdm import tqdm
from typing import Tuple, List, Optional, Dict, Text, Any
from scipy.sparse import csr_matrix

import rasa.utils.io
from rasa.core import utils
from rasa.core.actions.action import ACTION_LISTEN_NAME
from rasa.core.domain import PREV_PREFIX, Domain
from rasa.core.events import ActionExecuted
from rasa.core.trackers import DialogueStateTracker
from rasa.core.training.data import DialogueTrainingData
from rasa.nlu.featurizers.sparse_featurizer.count_vectors_featurizer import (
    CountVectorsFeaturizer,
)
from rasa.nlu.constants import CLS_TOKEN
from rasa.nlu.training_data import Message, TrainingData
from rasa.utils.common import is_logging_disabled

logger = logging.getLogger(__name__)


class SingleStateFeaturizer:
    """Base class for mechanisms to transform the conversations state into ML formats.

    Subclasses of SingleStateFeaturizer decide how the bot will transform
    the conversation state to a format which a classifier can read:
    feature vector.
    """

    def prepare_from_domain(self, domain: Domain) -> None:
        """Helper method to init based on domain."""

        pass

    def encode(self, state: Dict[Text, float]) -> np.ndarray:
        """Encode user input."""

        raise NotImplementedError(
            "SingleStateFeaturizer must have "
            "the capacity to "
            "encode states to a feature vector"
        )

    @staticmethod
    def action_as_one_hot(action: Text, domain: Domain) -> np.ndarray:
        """Encode system action as one-hot vector."""

        if action is None:
            return np.ones(domain.num_actions, dtype=int) * -1

        y = np.zeros(domain.num_actions, dtype=int)
        y[domain.index_for_action(action)] = 1
        return y

    def create_encoded_all_actions(self, domain: Domain) -> np.ndarray:
        """Create matrix with all actions from domain encoded in rows."""

        raise NotImplementedError("Featurizer must implement encoding actions.")


class BOWSingleStateFeaturizer(CountVectorsFeaturizer, SingleStateFeaturizer):
    def __init__(
        self
    ) -> None:

        super().__init__()
        self.delimiter = '_'

    def prepare_training_data_and_train(self, trackers_as_states):
        """
        Trains the vertorizers from real data;
        Preliminary for when the featurizer is to be used for raw text; 
        Args:
             - trackers_as_states: real data as a dictionary
             - delimiter: symbol to be used to divide into words
        """
        # TODO: 
        # - no delimiter
        # - how to avoid using NLU pipeline? 

        training_data = []
        for tracker in trackers_as_states:
            for state in tracker:
                if state:
                    state_keys = list(state.keys())
                    state_keys = [
                        Message(key.replace(self.delimiter, " ") + " " + CLS_TOKEN)
                        for key in state_keys
                    ]
                    training_data += state_keys

        training_data = TrainingData(training_examples=training_data)
        self.train(training_data)

    def prepare_from_domain(self, domain: Domain) -> None:
        """
        Train the fecturizers based on the inputs gotten from the domain;
        Args:
            - domain: domain file
        """

        training_data = domain.input_states
        training_data = [
            Message(key.replace(self.delimiter, " ") + " "+ CLS_TOKEN) for key in training_data
        ]
        training_data = TrainingData(training_examples=training_data)
        self.train(training_data)

    def encode(self, state: Dict[Text, float], type_output="dense"):
        """
        Encode the state into a numpy array or a sparse sklearn

        Args:
            - state: dictionary describing current state
            - type output: type to return the features as (numpyarray or sklearn coo_matrix)
        Returns:
            - nparray(vocab_size,) or coo_matrix(1, vocab_size) 
        """

        if state is None:
            if type_output == "sparse":
                return csr_matrix(
                    np.ones(len(self.vectorizers["text"].vocabulary_), dtype=np.int32) * -1
                )
            else:
                return (
                    np.ones(len(self.vectorizers["text"].vocabulary_), dtype=np.int32) * -1
                )


        state_keys = [key.replace(self.delimiter, " ") for key in list(state.keys())]
        attribute = "text"
        message = Message(" ".join(state_keys))
        message_tokens = self._get_processed_message_tokens_by_attribute(
            message, attribute
        )
        # features shape (1, seq, dim)
        features = self._create_sequence(attribute, [message_tokens + [CLS_TOKEN]])

        if type_output == "dense":
            return features[0].A[-1].astype(np.int32)
        else:
            return features[0].getrow(-1)

class TrackerFeaturizer:
    """Base class for actual tracker featurizers."""

    def __init__(
        self,
        state_featurizer: Optional[SingleStateFeaturizer] = None,
        use_intent_probabilities: bool = False,
    ) -> None:

        self.state_featurizer = state_featurizer
        self.use_intent_probabilities = use_intent_probabilities

    def _create_states(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
        is_binary_training: bool = False,
    ) -> List[Dict[Text, float]]:
        """Create states: a list of dictionaries.

        If use_intent_probabilities is False (default behaviour),
        pick the most probable intent out of all provided ones and
        set its probability to 1.0, while all the others to 0.0.
        """

        states = tracker.past_states(domain)

        # during training we encounter only 1 or 0
        if not self.use_intent_probabilities and not is_binary_training:
            bin_states = []
            for state in states:
                # copy state dict to preserve internal order of keys
                bin_state = dict(state)
                best_intent = None
                best_intent_prob = -1.0
                for state_name, prob in state:
                    if state_name.startswith("intent_"):
                        if prob > best_intent_prob:
                            # finding the maximum confidence intent
                            if best_intent is not None:
                                # delete previous best intent
                                del bin_state[best_intent]
                            best_intent = state_name
                            best_intent_prob = prob
                        else:
                            # delete other intents
                            del bin_state[state_name]

                if best_intent is not None:
                    # set the confidence of best intent to 1.0
                    bin_state[best_intent] = 1.0

                bin_states.append(bin_state)
            return bin_states
        else:
            return [dict(state) for state in states]

    def _pad_states(self, states: List[Any]) -> List[Any]:
        """Pads states."""

        return states

    def _featurize_states(
        self, trackers_as_states: List[List[Dict[Text, float]]]
    ) -> Tuple[np.ndarray, List[int]]:
        """Create X."""

        features = []
        true_lengths = []

        for tracker_states in trackers_as_states:
            dialogue_len = len(tracker_states)

            # len(trackers_as_states) = 1 means
            # it is called during prediction or we have
            # only one story, so no padding is needed

            if len(trackers_as_states) > 1:
                tracker_states = self._pad_states(tracker_states)

            story_features = [
                self.state_featurizer.encode(state) for state in tracker_states
            ]

            features.append(story_features)
            true_lengths.append(dialogue_len)

        # noinspection PyPep8Naming
        X = np.array(features)

        return X, true_lengths

    def _featurize_labels(
        self, trackers_as_actions: List[List[Text]], domain: Domain
    ) -> np.ndarray:
        """Create y."""

        labels = []
        for tracker_actions in trackers_as_actions:

            if len(trackers_as_actions) > 1:
                tracker_actions = self._pad_states(tracker_actions)

            story_labels = [
                self.state_featurizer.action_as_one_hot(action, domain)
                for action in tracker_actions
            ]

            labels.append(story_labels)

        y = np.array(labels)
        if y.ndim == 3 and isinstance(self, MaxHistoryTrackerFeaturizer):
            # if it is MaxHistoryFeaturizer, remove time axis
            y = y[:, 0, :]

        return y

    def _postprocess_trackers_as_states(
        self, trackers_as_states: List[List[Text]]
    ) -> List[List[Text]]:

        for i in range(len(trackers_as_states)):
            previous_intent = None
            for j in range(len(trackers_as_states[i])):

                if trackers_as_states[i][j]:
                    state_keys = list(trackers_as_states[i][j].keys())
                    current_intent = [
                        key for key in state_keys if key.startswith("intent_")
                    ]

                    if not current_intent == []:
                        current_intent = current_intent[0]
                        if current_intent == previous_intent:
                            del trackers_as_states[i][j][current_intent]
                        else:
                            previous_intent = current_intent

    def training_states_and_actions(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> Tuple[List[List[Dict]], List[List[Text]]]:
        """Transforms list of trackers to lists of states and actions."""

        raise NotImplementedError(
            "Featurizer must have the capacity to encode trackers to feature vectors"
        )

    def featurize_trackers(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> DialogueTrainingData:
        """Create training data."""

        if self.state_featurizer is None:
            raise ValueError(
                "Variable 'state_featurizer' is not set. Provide "
                "'SingleStateFeaturizer' class to featurize trackers."
            )

        self.state_featurizer.prepare_from_domain(domain)

        (trackers_as_states, trackers_as_actions) = self.training_states_and_actions(
            trackers, domain
        )

        self._postprocess_trackers_as_states(trackers_as_states)

        # noinspection PyPep8Naming
        X, true_lengths = self._featurize_states(trackers_as_states)
        y = self._featurize_labels(trackers_as_actions, domain)

        return DialogueTrainingData(X, y, true_lengths)

    def prediction_states(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> List[List[Dict[Text, float]]]:
        """Transforms list of trackers to lists of states for prediction."""

        raise NotImplementedError(
            "Featurizer must have the capacity to create feature vector"
        )

    # noinspection PyPep8Naming
    def create_X(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> np.ndarray:
        """Create X for prediction."""

        trackers_as_states = self.prediction_states(trackers, domain)
        X, _ = self._featurize_states(trackers_as_states)
        return X

    def persist(self, path) -> None:
        featurizer_file = os.path.join(path, "featurizer.json")
        rasa.utils.io.create_directory_for_file(featurizer_file)

        # noinspection PyTypeChecker
        rasa.utils.io.write_text_file(str(jsonpickle.encode(self)), featurizer_file)

    @staticmethod
    def load(path) -> Optional["TrackerFeaturizer"]:
        """Loads the featurizer from file."""

        featurizer_file = os.path.join(path, "featurizer.json")
        if os.path.isfile(featurizer_file):
            return jsonpickle.decode(rasa.utils.io.read_file(featurizer_file))
        else:
            logger.error(
                "Couldn't load featurizer for policy. "
                "File '{}' doesn't exist.".format(featurizer_file)
            )
            return None


class FullDialogueTrackerFeaturizer(TrackerFeaturizer):
    """Creates full dialogue training data for time distributed architectures.

    Creates training data that uses each time output for prediction.
    Training data is padded up to the length of the longest dialogue with -1.
    """

    def __init__(
        self,
        state_featurizer: SingleStateFeaturizer,
        use_intent_probabilities: bool = False,
    ) -> None:

        super().__init__(state_featurizer, use_intent_probabilities)
        self.max_len = None

    @staticmethod
    def _calculate_max_len(trackers_as_actions) -> Optional[int]:
        """Calculate the length of the longest dialogue."""

        if trackers_as_actions:
            return max([len(states) for states in trackers_as_actions])
        else:
            return None

    def _pad_states(self, states: List[Any]) -> List[Any]:
        """Pads states up to max_len."""

        if len(states) < self.max_len:
            states += [None] * (self.max_len - len(states))

        return states

    def training_states_and_actions(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> Tuple[List[List[Dict]], List[List[Text]]]:
        """Transforms list of trackers to lists of states and actions.

        Training data is padded up to the length of the longest dialogue with -1.
        """

        trackers_as_states = []
        trackers_as_actions = []

        logger.debug(
            "Creating states and action examples from "
            "collected trackers (by {}({}))..."
            "".format(type(self).__name__, type(self.state_featurizer).__name__)
        )
        pbar = tqdm(trackers, desc="Processed trackers", disable=is_logging_disabled())
        for tracker in pbar:
            states = self._create_states(tracker, domain, is_binary_training=True)

            delete_first_state = False
            actions = []
            for event in tracker.applied_events():
                if isinstance(event, ActionExecuted):
                    if not event.unpredictable:
                        # only actions which can be
                        # predicted at a stories start
                        actions.append(event.action_name)
                    else:
                        # unpredictable actions can be
                        # only the first in the story
                        if delete_first_state:
                            raise Exception(
                                "Found two unpredictable "
                                "actions in one story."
                                "Check your story files."
                            )
                        else:
                            delete_first_state = True

            if delete_first_state:
                states = states[1:]

            trackers_as_states.append(states[:-1])
            trackers_as_actions.append(actions)

        self.max_len = self._calculate_max_len(trackers_as_actions)
        logger.debug(f"The longest dialogue has {self.max_len} actions.")

        return trackers_as_states, trackers_as_actions

    def prediction_states(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> List[List[Dict[Text, float]]]:
        """Transforms list of trackers to lists of states for prediction."""

        trackers_as_states = [
            self._create_states(tracker, domain) for tracker in trackers
        ]

        return trackers_as_states


class MaxHistoryTrackerFeaturizer(TrackerFeaturizer):
    """Slices the tracker history into max_history batches.

    Creates training data that uses last output for prediction.
    Training data is padded up to the max_history with -1.
    """

    MAX_HISTORY_DEFAULT = 5

    def __init__(
        self,
        state_featurizer: Optional[SingleStateFeaturizer] = None,
        max_history: Optional[int] = None,
        remove_duplicates: bool = True,
        use_intent_probabilities: bool = False,
    ) -> None:

        super().__init__(state_featurizer, use_intent_probabilities)
        self.max_history = max_history or self.MAX_HISTORY_DEFAULT
        self.remove_duplicates = remove_duplicates

    @staticmethod
    def slice_state_history(
        states: List[Dict[Text, float]], slice_length: int
    ) -> List[Optional[Dict[Text, float]]]:
        """Slices states from the trackers history.

        If the slice is at the array borders, padding will be added to ensure
        the slice length.
        """

        slice_end = len(states)
        slice_start = max(0, slice_end - slice_length)
        padding = [None] * max(0, slice_length - slice_end)
        # noinspection PyTypeChecker
        state_features = padding + states[slice_start:]
        return state_features

    @staticmethod
    def _hash_example(states, action) -> int:
        """Hash states for efficient deduplication."""

        frozen_states = tuple(s if s is None else frozenset(s.items()) for s in states)
        frozen_actions = (action,)
        return hash((frozen_states, frozen_actions))

    def training_states_and_actions(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> Tuple[List[List[Optional[Dict[Text, float]]]], List[List[Text]]]:
        """Transforms list of trackers to lists of states and actions.

        Training data is padded up to the max_history with -1.
        """

        trackers_as_states = []
        trackers_as_actions = []

        # from multiple states that create equal featurizations
        # we only need to keep one.
        hashed_examples = set()

        logger.debug(
            "Creating states and action examples from "
            "collected trackers (by {}({}))..."
            "".format(type(self).__name__, type(self.state_featurizer).__name__)
        )
        pbar = tqdm(trackers, desc="Processed trackers", disable=is_logging_disabled())
        for tracker in pbar:
            states = self._create_states(tracker, domain, True)

            idx = 0
            for event in tracker.applied_events():
                if isinstance(event, ActionExecuted):
                    if not event.unpredictable:
                        # only actions which can be
                        # predicted at a stories start
                        sliced_states = self.slice_state_history(
                            states[: idx + 1], self.max_history
                        )

                        if self.remove_duplicates:
                            hashed = self._hash_example(
                                sliced_states, event.action_name
                            )

                            # only continue with tracker_states that created a
                            # hashed_featurization we haven't observed
                            if hashed not in hashed_examples:
                                hashed_examples.add(hashed)
                                trackers_as_states.append(sliced_states)
                                trackers_as_actions.append([event.action_name])
                        else:
                            trackers_as_states.append(sliced_states)
                            trackers_as_actions.append([event.action_name])

                        pbar.set_postfix(
                            {"# actions": "{:d}".format(len(trackers_as_actions))}
                        )
                    idx += 1

        logger.debug("Created {} action examples.".format(len(trackers_as_actions)))

        return trackers_as_states, trackers_as_actions

    def prediction_states(
        self, trackers: List[DialogueStateTracker], domain: Domain
    ) -> List[List[Dict[Text, float]]]:
        """Transforms list of trackers to lists of states for prediction."""

        trackers_as_states = [
            self._create_states(tracker, domain) for tracker in trackers
        ]
        trackers_as_states = [
            self.slice_state_history(states, self.max_history)
            for states in trackers_as_states
        ]

        return trackers_as_states
