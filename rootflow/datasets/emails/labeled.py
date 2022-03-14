from typing import List

import os
from os.path import exists
from tqdm import tqdm
import json
import zarr

from rootflow.datasets.base.dataset import RootflowDatasetView
from rootflow.datasets.base import RootflowDataset, RootflowDataItem


class LabeledEmails(RootflowDataset):
    """Labeled emails from the Branch email corpus.

    Inherits from :class:`RootflowDataset`.
    All emails from the branch corpus which were labeled by an oracle. Targets are
    binary classifications indicating if the particular labeler would have liked to
    receive that email as a notification. The specific prompt was as follows:
        "If you received this email today, would you want to receive a notification
        or alert about the email, based on its contents?"
    """

    BUCKET = "rootflow"
    DATASET_PATH = "datasets/email-notification"
    FILE_NAME = "emails.zarr"
    CHUNK_SIZE = 50
    DATA_DELIMITER = "$$$data-separator$$$"
    LABEL_ENCODING = {"False": 0, "True": 1}

    def __init__(
        self, prefix: str = "", root: str = None, download: bool = None, tasks=None
    ) -> None:
        self.prefix = self.DATASET_PATH + prefix
        super().__init__(root, download, tasks)

    def download(self, directory: str):
        from google.cloud import storage

        file_path = os.path.join(directory, self.FILE_NAME)
        print(file_path)
        storage_client = storage.Client()

        # count how many files we are going to download
        bucket = storage.Bucket(storage_client, name=self.BUCKET)
        file_names_iter = storage_client.list_blobs(bucket, prefix=self.prefix)
        num_files = sum(1 for blob in file_names_iter)

        if not exists(file_path):
            store = zarr.NestedDirectoryStore(file_path)
            self.data = zarr.create(
                shape=num_files, chunks=self.CHUNK_SIZE, store=store, dtype=str
            )
        else:
            self.data = zarr.open(
                file_path, mode="a", shape=num_files, chunks=self.CHUNK_SIZE, dtype=str
            )

        print("Downloading files")
        file_names_iter = storage_client.list_blobs(bucket, prefix=self.prefix)
        # to keep the zarr of resizing so much we insert a chunk at a time
        # dynamic array could be switched with linked list to avoid resizing
        temp_store = []
        store_index = 0
        zarr_initial_index = 0
        bytes_loaded = 0
        for i, file in tqdm(enumerate(file_names_iter), total=num_files, smoothing=0.9):
            data = file.download_as_string()
            json_object = json.loads(data)

            # formatt the data
            data_dict = {
                "from": json_object["data"]["from"],
                "subject": json_object["data"]["subject"],
                "mbox": json_object["data"]["mbox"],
            }
            full_item = {
                "id": json_object["label_info"]["example_id"],
                "data": data_dict,
                "target": json_object["label_info"]["label"],
                "oracle_id": json_object["label_info"]["oracle_id"],
                "group_id": json_object["label_info"]["dataset_id"],
            }
            bytes_loaded += len(full_item)
            # full_item = json_object["label_info"]["example_id"] + self.DATA_DELIMITER + json_object["data"]["from"] + self.DATA_DELIMITER
            # full_item += json_object["data"]["subject"] + self.DATA_DELIMITER + json_object["data"]["mbox"]

            if len(temp_store) < self.CHUNK_SIZE:
                temp_store.append(str(full_item))
                store_index += 1
            elif len(temp_store) == self.CHUNK_SIZE and store_index < self.CHUNK_SIZE:
                temp_store[store_index] = full_item
                store_index += 1
            else:
                # insert full chunk into zarr
                self.data[zarr_initial_index:i] = temp_store
                zarr_initial_index = i
                temp_store[0] = full_item
                store_index = 1

            if i == num_files - 1:
                # could have a partial chunk so we insert now
                self.data[zarr_initial_index : i + 1] = temp_store[0:store_index]

    def prepare_data(self, directory: str) -> List["RootflowDataItem"]:
        self.root = directory
        file_path = os.path.join(directory, self.FILE_NAME)
        if exists(file_path):
            zarr_file = zarr.open(file_path, mode="r+")
            data_items = []

            for encoded_string in tqdm(zarr_file, total=len(zarr_file)):
                id, mbox, label, oracle_id, dataset_id = encoded_string.split(
                    self.DATA_DELIMITER
                )
                if label == "":
                    continue

                data = {"mbox": mbox, "oracle_id": oracle_id}
                data_item = RootflowDataItem(
                    data, id=id, target=self.LABEL_ENCODING[label]
                )
                data_items.append(data_item)

            return data_items
        else:
            return FileNotFoundError

    def split_by_oracle_id(self) -> List[RootflowDataset]:
        """Splits dataset into a list of datasets.

        Creates a dataset for each labeler (oracle) in the Branch email corpus. The
        datasets are views, so no data is duplicated.

        Returns:
            List[RootflowDataset] : A list of the datasets, split by oracle_id.
        """
        oracle_indices = {}
        for idx, data_item in self.data:
            oracle_id = data_item.data["oracle_id"]
            oracle_indices[oracle_id].append(idx)
        oracle_datasets = [
            RootflowDatasetView(self, indices) for indices in oracle_indices.values()
        ]
        return oracle_datasets

    def index(self, index: int) -> tuple:
        data_item = self.data[index]
        id, data, target = data_item.id, data_item.data, data_item.target
        data = data["mbox"]
        return (id, data, target)


if __name__ == "__main__":
    dataset = LabeledEmails()