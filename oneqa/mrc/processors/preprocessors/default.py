import itertools
from lib2to3.pgen2.tokenize import tokenize
import random
import uuid
from operator import sub
from typing import List, Iterable, Tuple, Any, Dict

from datasets.arrow_dataset import Batch
from transformers import BatchEncoding
from datasets import Dataset
from datasets.features.features import Sequence, Value

from oneqa.mrc.processors.preprocessors.abstract import AbstractPreProcessor
from oneqa.mrc.data_models.subsample_type import SubsampleType
from oneqa.mrc.data_models.target_type import TargetType


# TODO type signatures for all methods
class DefaultPreProcessor(AbstractPreProcessor):  # todo better name?
    _del_keys = ["overflow_to_sample_mapping"] #, "offset_mapping"]
    _feature_types = {'question': Value(dtype='string', id=None),
                      'context': Sequence(feature=Value(dtype='string', id=None), length=-1, id=None)}
    _train_feature_types = {
        'target': {'start_positions': Sequence(feature=Value(dtype='int64', id=None), length=-1, id=None),
                   'end_positions': Sequence(feature=Value(dtype='int64', id=None), length=-1, id=None),
                   'passage_indices': Sequence(feature=Value(dtype='int64', id=None), length=-1, id=None),
                   'yes_no_answer': Sequence(feature=Value(dtype='string', id=None), length=-1, id=None)}}
    # _train_feature_types = {   # TODO: switch this layout to match tydi
    #     'annotations': Sequence(feature={
    #         'start_positions': Value(dtype='int32', id=None),
    #         'end_positions': Value(dtype='int32', id=None),
    #         'passage_indices': Value(dtype='int32', id=None),
    #         'yes_no_answer': Value(dtype='string', id=None)})
    # }
    _example_id_type = {'example_id': Value(dtype='string', id=None)}

    def adapt_dataset(self, dataset: Dataset, is_train: bool) -> Dataset:
        if 'example_id' not in dataset.features:
            # example_id = [str(uuid.uuid4()) for _ in range(dataset.num_rows)]
            # dataset = dataset.add_column('example_id', example_id)
            dataset = dataset.map(  # Map instead of add column to allow caching
                self._insert_example_ids,
                batched=True,
                load_from_cache_file=self._load_from_cache_file,
                num_proc=self._num_workers,
            )
        # self.validate_schema(dataset, is_train, pre_adaptation=False)  # TODO: re-enable
        return dataset

    @staticmethod
    def _insert_example_ids(examples: Batch) -> Batch:
        n_examples = len(examples['question'])
        example_id = [str(uuid.uuid4()) for _ in range(n_examples)]
        examples['example_id'] = example_id
        return examples

    def process_train(self, examples: Dataset) -> Tuple[Dataset, Dataset]:
        return self._process(examples, is_train=True)

    def process_eval(self, examples: Dataset) -> Tuple[Dataset, Dataset]:
        return self._process(examples, is_train=False)

    def _process(self, examples: Dataset, is_train: bool) -> Tuple[Dataset, Dataset]:
        examples = self.adapt_dataset(examples, is_train)
        if examples.num_rows == 0:
            raise ValueError("No examples to process")

        features = examples.map(
            self._process_batch,
            fn_kwargs=dict(is_train=is_train),
            batched=True,
            with_indices=True,
            num_proc=self._num_workers,
            remove_columns=examples.column_names,
            load_from_cache_file=self._load_from_cache_file,
            desc=f"Running tokenizer on {'train' if is_train else 'eval'} dataset",
            # features=self._create_features_schema(is_train),
        )
        if is_train:
            features = self.subsample_features(features)
        return examples, features

    def _process_batch(self, examples: Batch, indices: List[int], is_train: bool) -> BatchEncoding:
        examples_question = examples['question']
        examples_context = examples['context']
        if isinstance(examples_question, str):  # wrap single (question, [context]) pair in list
            examples_question = [examples_question]
            examples_context = [examples_context]
        examples_question = [q.lstrip()[:self._max_q_char_len] for q in examples_question]
        
        # create 1:1 question:context lists
        expanded_examples_question = []
        expanded_examples_idx = []
        for i, (question, context) in enumerate(zip(examples_question, examples_context)):
            n_context_for_example = len(context)
            if self._single_context_multiple_passages and n_context_for_example != 1:
                raise ValueError("Must have exactly one context for each question to use single_context_multiple_passages")
            expanded_examples_question.extend(itertools.repeat(question, n_context_for_example))
            expanded_examples_idx.extend(itertools.repeat(i, n_context_for_example))
        expanded_examples_context = list(itertools.chain.from_iterable(examples_context))

        tokenized_examples = self._tokenizer(
            expanded_examples_question if self._pad_on_right else expanded_examples_context,
            expanded_examples_context if self._pad_on_right else expanded_examples_question,
            stride=self._stride,
            max_length=self._max_seq_len,
            truncation='only_second' if self._pad_on_right else 'only_first',
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
        )

        tokenized_examples['example_idx'] = [expanded_examples_idx[oidx] for oidx in tokenized_examples["overflow_to_sample_mapping"]]
        tokenized_examples['example_id'] = [examples['example_id'][eidx] for eidx in tokenized_examples['example_idx']]

        if self._single_context_multiple_passages:
            # context_idx = self._assign_context_idxs_to_single_passage(tokenized_examples, examples)
            pass
        else:
            spans_per_example = self._generate_previous_spans_per_example(tokenized_examples['example_idx'], tokenized_examples["overflow_to_sample_mapping"])
            tokenized_examples['context_idx'] = list(map(sub, tokenized_examples["overflow_to_sample_mapping"], spans_per_example))

        if is_train:
            tokenized_examples = self._create_train_targets(tokenized_examples, examples)
            tokenized_examples = self.label_features_for_subsampling(tokenized_examples, examples)
        else:
            tokenized_examples = self._create_eval_targets(tokenized_examples)

        tokenized_examples['example_idx'] = [indices[eidx] for eidx in tokenized_examples['example_idx']]

        for key in self._del_keys:
            tokenized_examples.pop(key, None)
        
        return tokenized_examples

    def _create_train_targets(self, tokenized_examples: BatchEncoding, examples: Batch) -> BatchEncoding:  # TODO: rename create targets fns?
        target = examples['target']

        # Since one context might give us several features if it has a long context, and each example can have many contexts,
        # we need a map from a feature to ts corresponding example. This key gives us just that.
        example_mapping = tokenized_examples['example_idx']
        # The offset mappings will give us a map from token to character position in the original context. This will
        # help us compute the start_positions and end_positions.
        offset_mapping = tokenized_examples["offset_mapping"]

        # Let's label those examples!
        tokenized_examples["start_positions"] = []
        tokenized_examples["end_positions"] = []
        tokenized_examples["target_type"] = []

        for i, offsets in enumerate(offset_mapping):
            # We will label impossible answers with the index of the CLS token.
            input_ids = tokenized_examples["input_ids"][i]
            cls_index = input_ids.index(self._tokenizer.cls_token_id)

            # Grab the sequence corresponding to that example (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)

            # One example can give several spans, this is the index of the example containing this span of text.
            example_index = example_mapping[i]
            passage_candidates = examples['passage_answer_candidates'][example_index]
            t = target[example_index]
            passage_index = t['passage_indices'][0]
            start_position = t['start_positions'][0]
            end_position = t['end_positions'][0]
            yes_no_answer = TargetType.from_bool_label(t['yes_no_answer'][0])

            # Start/end character index of the answer in the text.
            start_char = start_position
            end_char = end_position

            # Start token index of the current span in the text.
            token_start_index = 0
            while sequence_ids[token_start_index] != (1 if self._pad_on_right else 0):
                token_start_index += 1

            # End token index of the current span in the text.
            token_end_index = len(input_ids) - 1
            while sequence_ids[token_end_index] != (1 if self._pad_on_right else 0):
                token_end_index -= 1

            if passage_index == -1:
                window_contains_correct_passage = False
            else:
                if self._single_context_multiple_passages:
                    # for i in range(len(passage_candidates['plaintext_start_byte'])):
                    #     psb = passage_candidates['plaintext_start_byte'][i]
                    #     peb = passage_candidates['plaintext_end_byte'][i]
                    #     if self._spans_intersect((psb, peb), (offsets[token_start_index][0], offsets[token_end_index][1])):
                    #     # if psb <= offsets[token_start_index][0] <= peb or psb <= offsets[token_end_index][1] <= peb:
                    #         window_contains_correct_passage = True
                    #         break
                    # else:
                    #     window_contains_correct_passage = False

                    psb = passage_candidates['plaintext_start_byte'][passage_index]
                    peb = passage_candidates['plaintext_end_byte'][passage_index]
                    window_contains_correct_passage = self._spans_intersect((psb, peb), (offsets[token_start_index][0], offsets[token_end_index][1]))
                else:
                    context_idx = tokenized_examples['context_idx'][i]
                    window_contains_correct_passage = passage_index == context_idx

            if window_contains_correct_passage and start_position == -1:  # Passage or Y/N Answer
                tokenized_examples["start_positions"].append(cls_index)
                tokenized_examples["end_positions"].append(cls_index)
                tt = yes_no_answer
                if tt not in (TargetType.YES, TargetType.NO):
                    tt = TargetType.PASSAGE_ANSWER
                tokenized_examples["target_type"].append(tt)
            elif not window_contains_correct_passage or start_position == -1:  # No Answer
                tokenized_examples["start_positions"].append(cls_index)
                tokenized_examples["end_positions"].append(cls_index)
                tokenized_examples["target_type"].append(TargetType.NO_ANSWER)
            else:  # Span Answer
                # Detect if the answer is out of the span (in which case this feature is labeled with the CLS index).
                if not (offsets[token_start_index][0] <= start_char and offsets[token_end_index][1] >= end_char):
                    tokenized_examples["start_positions"].append(cls_index)
                    tokenized_examples["end_positions"].append(cls_index)
                    tokenized_examples["target_type"].append(TargetType.PASSAGE_ANSWER)
                else:
                    # Otherwise move the token_start_index and token_end_index to the two ends of the answer.
                    # Note: we could go after the last offset if the answer is the last word (edge case).
                    while token_start_index < len(offsets) and offsets[token_start_index][0] <= start_char:
                        token_start_index += 1
                    tokenized_examples["start_positions"].append(token_start_index - 1)
                    while offsets[token_end_index][1] >= end_char:
                        token_end_index -= 1
                    tokenized_examples["end_positions"].append(token_end_index + 1)
                    tokenized_examples["target_type"].append(TargetType.SPAN_ANSWER)

        return tokenized_examples

    def _create_eval_targets(self, tokenized_examples: BatchEncoding) -> BatchEncoding:
        
        context_index = 1 if self._pad_on_right else 0
        for i in range(len(tokenized_examples["input_ids"])):
            # Grab the sequence corresponding to that example (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)

            # Set to None the offset_mapping that are not part of the context so it's easy to determine if a token
            # position is part of the context or not.
            tokenized_examples["offset_mapping"][i] = [
                (o if sequence_ids[k] == context_index else None)
                for k, o in enumerate(tokenized_examples["offset_mapping"][i])
            ]

        return tokenized_examples
    
    def label_features_for_subsampling(self, tokenized_examples: BatchEncoding, examples: Batch) -> BatchEncoding:
        if self._subsample_all_features:
            self._logger.warning("Keeping all negative training instances -- "
                                 "this may create an unbalanced training set and increase training time significantly")
            return tokenized_examples
        elif self._subsample_no_features:
            self._logger.warning("Removing all negative training instances -- only positives will be used")

        example_mapping = tokenized_examples['example_idx']

        tokenized_examples['subsample_type'] = []
        for i in range(len(tokenized_examples['input_ids'])):
            if tokenized_examples['target_type'][i] != TargetType.NO_ANSWER:
                st = SubsampleType.POSITIVE
            else:
                example_idx = example_mapping[i]
                passage_mapping = examples['target'][example_idx]['passage_indices']
                passage_idx = passage_mapping[0]
                has_answer = passage_idx != -1
                if has_answer:
                    st = SubsampleType.NEGATIVE_HAS_ANSWER
                else:
                    st = SubsampleType.NEGATIVE_NO_ANSWER
            tokenized_examples['subsample_type'].append(st)

        return tokenized_examples

    def subsample_features(self, dataset: Dataset) -> Dataset:
        if self._subsample_all_features:
            return dataset

        keep_indices = [i for i, st in enumerate(dataset['subsample_type']) if self._keep_feature(st)]
        try:
            dataset = dataset.select(keep_indices)
        except IndexError as ex:
            raise ValueError("No features remaining after subsampling") from ex
        dataset = dataset.remove_columns('subsample_type')
        return dataset

    def _keep_feature(self, st: SubsampleType) -> bool:
        if st == SubsampleType.POSITIVE:
            return True
        elif st == SubsampleType.NEGATIVE_HAS_ANSWER:
            return random.random() < self._negative_sampling_prob_when_has_answer
        elif st == SubsampleType.NEGATIVE_NO_ANSWER:
            return random.random() < self._negative_sampling_prob_when_no_answer
        else:
            raise NotImplementedError(f"Unexpected subsample type: {st}")

    @property
    def _pad_on_right(self) -> bool:
        return self._tokenizer.padding_side == "right"

    @property
    def _subsample_all_features(self) -> bool:
        return self._negative_sampling_prob_when_has_answer == self._negative_sampling_prob_when_no_answer == 1.

    @property
    def _subsample_no_features(self) -> bool:
        return self._negative_sampling_prob_when_has_answer == self._negative_sampling_prob_when_no_answer == 0.

    @staticmethod
    def _generate_previous_spans_per_example(example_idx: List[int], sample_mapping: List[int]) -> Iterable[int]:
        group_start_idx = 0
        for _, group in itertools.groupby(example_idx):
            group_len = None
            for group_len, _ in enumerate(group, 1):
                pass
            if group_len is None:  # this should never be triggered
                raise ValueError("Unexpected group length None")
            yield from itertools.repeat(sample_mapping[group_start_idx], group_len)
            group_start_idx += group_len

    def validate_schema(self, dataset: Dataset, is_train: bool, pre_adaptation: bool = True) -> None:
        cls = type(self) if pre_adaptation else DefaultPreProcessor
        items = cls._feature_types.items()
        if is_train:
            items = itertools.chain(items, cls._train_feature_types.items())
        if not pre_adaptation:
            items = itertools.chain(items, cls._example_id_type.items())
        for feature_name, feature_type in items:
            if feature_name not in dataset.features:
                raise ValueError(f"Expected but did not find feature '{feature_name}' in dataset")
            elif dataset.features[feature_name] != feature_type:
                raise ValueError(F"Feature type mismatch for feature '{feature_name}'. "
                                 F"Expected {feature_type} but found {dataset.features[feature_name]}")

        # if not pre_adaptation:
        #     if dataset.features['example_id'] != self._example_id_type:
        #         raise ValueError(F"Feature type mismatch for feature 'example_id'. "
        #                          F"Expected {self._example_id_type} but found {dataset.features['example_id']}")

    # def _create_features_schema(self, is_train: bool) -> Features:
    #     schema = Features()
    #     schema = schema.update(self._feature_types)
    #     schema['example_id'] = self._example_id_type
    #     if is_train:
    #         schema.update(self._train_feature_types)
    #     return schema

    # def _generate_previous_spans_per_example(self, tokenized_examples: BatchEncoding, examples: Batch) -> List[int]:
    #     context_idx = [-1] * len(tokenized_examples["overflow_to_sample_mapping"])

    @staticmethod
    def _spans_intersect(s1: Tuple[int, int], s2: Tuple[int, int]) -> bool:
        return (s1[0] <= s2[0] <= s1[1]) or (s2[0] <= s1[0] <= s2[1]) or \
               (s1[0] <= s2[1] <= s1[1]) or (s2[0] <= s1[1] <= s2[1])
