from typing import Callable, Mapping, Sequence, Tuple, List, Union
import logging
import random
import os
import torch
import numpy as np
from torch.utils.data import Dataset
import rootflow
from rootflow.datasets.display_utils import (
    format_docstring,
    format_examples_tabular,
    format_statistics,
)
from rootflow.datasets.utils import (
    batch_enumerate,
    get_nested_data_types,
    get_target_shape,
    map_functions,
    get_unique,
)


class FunctionalDataset(Dataset):
    def __init__(self) -> None:
        self.data_transforms = []
        self.target_transforms = []
        self.has_data_transforms = False
        self.has_target_transforms = False

    def __len__(self):
        raise NotImplementedError

    def __getitem__(self, index):
        if isinstance(index, int):
            id, data, target = self.index(index)
            return {"id": id, "data": data, "target": target}
        elif isinstance(index, slice):
            data_indices = list(range(len(self))[index])
            return RootflowDatasetView(self, data_indices, sorted=False)
        elif isinstance(index, (tuple, list)):
            return RootflowDatasetView(self, index)

    def __iter__(self):
        for index in range(len(self)):
            id, data, target = self.index(index)
            yield {"id": id, "data": data, "target": target}

    def index(self, index) -> tuple:
        raise NotImplementedError

    def split(
        self, test_proportion: float = 0.1, seed: int = None
    ) -> Tuple["RootflowDatasetView", "RootflowDatasetView"]:
        dataset_length = len(self)
        indices = list(range(dataset_length))
        random.Random(seed).shuffle(indices)
        n_test = int(dataset_length * test_proportion)
        return (
            RootflowDatasetView(self, indices[n_test:], sorted=False),
            RootflowDatasetView(self, indices[:n_test], sorted=False),
        )

    def map(
        self,
        function: Union[Callable, List[Callable]],
        targets: bool = False,
        batch_size: int = None,
    ) -> Union["RootflowDataset", "RootflowDatasetView"]:
        raise NotImplementedError

    def where(
        self,
        filter_function: Callable,
        targets: bool = False,
    ) -> "RootflowDatasetView":
        if targets:
            conditional_attr = "target"
        else:
            conditional_attr = "data"

        filtered_indices = []
        for index, item in enumerate(self):
            if filter_function(item[conditional_attr]):
                filtered_indices.append(index)
        return RootflowDatasetView(self, filtered_indices)

    def transform(
        self, function: Union[Callable, List[Callable]], targets: bool = False
    ) -> Union["RootflowDataset", "RootflowDatasetView"]:
        if not isinstance(function, (tuple, list)):
            function = [function]
        if targets:
            self.target_transforms += function
            self.has_target_transforms = True
        else:
            self.data_transforms += function
            self.has_data_transforms = True
        return self

    def __add__(self, object):
        if not isinstance(object, FunctionalDataset):
            raise AttributeError(f"Cannot add a dataset to {type(object)}")

        return ConcatRootflowDatasetView(self, object)

    def tasks(self):
        raise NotImplementedError

    def task_shapes(self):
        raise NotImplementedError

    def stats(self):
        data_example = self[0]["data"]
        target_example = self[0]["target"]
        return {
            "length": len(self),
            "data_types": get_nested_data_types(data_example),
            "target_types": get_nested_data_types(target_example),
        }

    def examples(self, num_examples: int = 5):
        num_examples = min(len(self), num_examples)
        return [self[i] for i in range(num_examples)]

    def describe(self, output_width: int = None):
        terminal_size = os.get_terminal_size()
        if output_width is None:
            description_width = min(150, terminal_size.columns)
        else:
            description_width = output_width

        print(f"{type(self).__name__}:")
        dataset_doc = type(self).__doc__
        if not dataset_doc is None:
            print(format_docstring(dataset_doc, description_width))
        else:
            print("(No Description)")

        print("\nStats:")
        print(format_statistics(self.stats(), description_width, indent=True))

        print("\nExamples:")
        print(format_examples_tabular(self.examples(), description_width, indent=True))


class RootflowDataset(FunctionalDataset):
    def __init__(self, root: str = None, download: bool = True) -> None:
        super().__init__()
        self.DEFAULT_DIRECTORY = os.path.join(
            rootflow.__location__, "datasets/data", type(self).__name__, "data"
        )
        if root is None:
            logging.info(
                f"{type(self).__name__} root is not set, using the default data root of {self.DEFAULT_DIRECTORY}"
            )
            root = self.DEFAULT_DIRECTORY

        try:
            self.data = self.prepare_data(root)
        except FileNotFoundError as e:
            logging.warning(f"Data could not be loaded from {root}.")
            if download:
                logging.info(
                    f"Downloading {type(self).__name__} data to location {root}."
                )
                if not os.path.exists(root):
                    os.makedirs(root)
                self.download(root)
                self.data = self.prepare_data(root)
            else:
                raise e
        logging.info(f"Loaded {type(self).__name__} from {root}.")

        self.setup()
        logging.info(f"Setup {type(self).__name__}.")

        # Memoization vars
        self._tasks = None
        self._task_shapes = None

    def prepare_data(self, path: str) -> List["RootflowDataItem"]:
        raise NotImplementedError

    def download(self, path: str):
        raise NotImplementedError

    def setup(self):
        pass

    def tasks(self):
        if self._tasks:
            return self._tasks
        example_target = self.index(0)[2]
        if isinstance(example_target, Mapping):
            self._tasks = list(example_target.keys())
        else:
            self._tasks = None
        return self._tasks

    def task_shapes(self):
        if self._task_shapes:
            return self._task_shapes
        example_target = self.index(0)[2]
        if isinstance(example_target, Mapping):
            shapes = {}
            for key, value in example_target.items():
                if isinstance(value, int):
                    shapes[key] = len({data_item["target"][key] for data_item in self})
                else:
                    shapes[key] = get_target_shape(value)
            self._task_shapes = shapes
        elif isinstance(example_target, Sequence) and not isinstance(
            example_target, str
        ):
            self._task_shapes = len(example_target)
        elif isinstance(example_target, (torch.Tensor, np.ndarray)):
            self._task_shapes = example_target.shape
        elif isinstance(example_target, int):
            self._task_shapes = len({data_item["target"] for data_item in self})
        elif isinstance(example_target, float):
            self._task_shapes = 1
        else:
            self._task_shapes = None
        return self._task_shapes

    def map(
        self,
        function: Union[Callable, List[Callable]],
        targets: bool = False,
        batch_size: int = None,
    ) -> Union["RootflowDataset", "RootflowDatasetView"]:
        # Represents some dangerous interior mutability
        # Does not play well with views (What should we change and not change. Do we allow different parts of the dataset to have different data?)
        # Does not play well with datasets who need to have data be memmaped from disk
        if targets:
            attribute = "target"
        else:
            attribute = "data"

        if batch_size is None:
            for idx, example in enumerate(self.data):
                data_item = self.data[idx]
                setattr(data_item, attribute, function(getattr(data_item, attribute)))
        else:
            for slice, batch in batch_enumerate(self.data, batch_size):
                mapped_batch_data = function(
                    [getattr(data_item, attribute) for data_item in batch]
                )
                for idx, mapped_example_data in zip(slice, mapped_batch_data):
                    data_item = self.data[idx]
                    setattr(data_item, attribute, mapped_example_data)

        return self

    def __len__(self) -> int:
        return len(self.data)

    def index(self, index):
        data_item = self.data[index]
        id, data, target = data_item.id, data_item.data, data_item.target
        if id is None:
            id = f"{type(self).__name__}-{index}"
        if self.has_data_transforms:
            data = map_functions(data, self.data_transforms)
        if self.has_target_transforms:
            target = map_functions(target, self.target_transforms)
        return (id, data, target)


class RootflowDatasetView(FunctionalDataset):
    def __init__(
        self,
        dataset: Union[RootflowDataset, "RootflowDatasetView"],
        view_indices: List[int],
        sorted=True,
    ) -> None:
        super().__init__()
        self.dataset = dataset
        unique_indices = get_unique(view_indices, ordered=sorted)
        self.data_indices = unique_indices

    def tasks(self):
        return self.dataset.tasks()

    def task_shapes(self):
        return self.dataset.task_shapes()

    def map(self, function: Callable, targets: bool = False, batch_size: int = None):
        raise AttributeError("Cannot map over a dataset view!")

    def __len__(self):
        return len(self.data_indices)

    def index(self, index):
        id, data, target = self.dataset.index(self.data_indices[index])
        if self.has_data_transforms:
            data = map_functions(data, self.data_transforms)
        if self.has_target_transforms:
            target = map_functions(target, self.target_transforms)
        return (id, data, target)


class ConcatRootflowDatasetView(FunctionalDataset):
    def __init__(
        self,
        datatset_one: Union[RootflowDataset, "RootflowDatasetView"],
        dataset_two: Union[RootflowDataset, "RootflowDatasetView"],
    ):
        super().__init__()
        self.dataset_one = datatset_one
        self.dataset_two = dataset_two
        self.transition_point = len(datatset_one)

    def map(self, function: Callable, targets: bool = False, batch_size: int = None):
        raise AttributeError("Cannot map over concatenated datasets!")

    def tasks(self):
        return self.dataset_one.tasks()

    def task_shapes(self):
        return self.dataset_two.task_shapes()

    def __len__(self):
        return len(self.dataset_one) + len(self.dataset_two)

    def index(self, index):
        if index < self.transition_point:
            selected_dataset = self.dataset_one
        else:
            selected_dataset = self.dataset_two
            index -= self.transition_point
        id, data, target = selected_dataset.index(index)
        if self.has_data_transforms:
            data = map_functions(data, self.data_transforms)
        if self.has_target_transforms:
            target = map_functions(target, self.target_transforms)
        return (id, data, target)


class RootflowDataItem:
    __slots__ = ("id", "data", "target")

    def __init__(self, data, id=None, target=None) -> None:
        self.data = data
        self.id = id
        self.target = target  # How do we differentiate between regression, single class or multiclass tasks?

    def __iter__(self):
        return iter((self.id, self.data, self.target))
