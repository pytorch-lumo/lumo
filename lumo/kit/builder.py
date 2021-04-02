from collections import OrderedDict
from copy import copy
from itertools import accumulate
from operator import add
from typing import Sized, Sequence, Union, Callable, Dict, Any, Optional, List

from torch.utils.data import Dataset, sampler, distributed

from .delegate import DataDelegate, Data, DelegateDataTypeError

__all__ = ['BaseBuilder', 'DatasetBuilder']


class BaseBuilder(Dataset):
    def __init__(self):
        self._check_delegate_flag = set()

        self._sampler = None
        self._batch_sampler = None
        self._chain = False
        self._sub_indices = None
        self._reindices = None
        self._dataloader_args = None

    def __repr__(self):
        return (f"{self.__class__.__name__}()")

    def __getitem__(self, index):
        raise NotImplementedError()

    def __len__(self):
        if self._sub_indices is not None:
            return len(self._sub_indices)
        return self.raw_len

    @property
    def raw_len(self):
        raise NotImplementedError()

    def _check_len(self, source: Sized):
        """
        Each source must have equal size, this function will check this, and an Exception will be raised
        if new source doesn't have the same size that added sources have.
        """
        len_ = len(source)
        if self._dataset_len is None:
            self._dataset_len = len_
        else:
            assert self._dataset_len == len(source), f"{self._dataset_len} != {len(source)}"

    def _map_index(self, index):
        """
        Map the raw index to the final index for source data.
        Args:
            index:

        Returns:

        """
        if self._sub_indices is not None:
            index = self._sub_indices[index]
        if self._reindices is not None:
            index = self._reindices[index]

        return index

    def copy(self):
        builder = DatasetBuilder()
        builder._sub_indices = copy(self._sub_indices)
        builder._reindices = copy(self._reindices)
        return builder

    def subset(self, indices: Sequence[int], copy=False):
        if copy:
            builder = self.copy()
            builder.subset(indices, copy=False)
            return builder

        self._sub_indices = indices
        return self

    def reindices(self, indices: Sequence[int]):
        self._reindices = indices
        return self

    def zip(self):
        self._chain = False
        return self

    def chain(self):
        self._chain = True
        return self

    def shuffle(self):
        import random
        ids = list(range(len(self.raw_len)))
        random.shuffle(ids)
        self._reindices = ids
        return self

    def random_sampler(self, replacement: bool = False, generator=None):
        _sampler = sampler.RandomSampler(self, replacement=replacement, generator=generator
                                         , num_samples=len(self))
        self.sample_by(_sampler)
        return self

    def weighted_random_sampler(self, weights: Sequence[float],
                                replacement: bool = True, generator=None):
        _sampler = sampler.WeightedRandomSampler(weights=weights,
                                                 num_samples=len(self),
                                                 replacement=replacement,
                                                 generator=generator)
        self.sample_by(_sampler)
        return self

    def sequential_sampler(self):
        _sampler = sampler.SequentialSampler(self)
        self.sample_by(_sampler)
        return self

    def sample_by(self, sampler: sampler.Sampler):
        self._sampler = sampler
        return self

    def batch_sample_by(self, batch_sampler: sampler.BatchSampler):
        self._batch_sampler = batch_sampler
        return self

    def sample_distributed(self, rank: Optional[int] = None,
                           num_replicas: Optional[int] = None,
                           shuffle: bool = True,
                           seed: int = 0, drop_last: bool = False):
        """
        Be sure call this in each subprocess respectively, not in main process, when train distributed.
        Args:
            see class `torch.util.data.distributed.DistributedSampler` for detail
        """
        source = self
        if self._sampler is not None:
            source = self._sampler
        _sampler = distributed.DistributedSampler(source, rank=rank, num_replicas=num_replicas,
                                                  shuffle=shuffle, seed=seed, drop_last=drop_last)
        self._sampler = _sampler
        return self

    def dataLoader(self, *args, **kwargs):
        self._dataloader_args = (args, kwargs)
        return self

    def build_dataloader(self):
        from torch.utils.data import DataLoader
        if self._dataloader_args is None:
            raise ValueError('Dataloader args not exists, call dataloader(*args,**kwargs) to pass parameters')
        args, kwargs = self._dataloader_args
        return DataLoader(self, *args, **kwargs)


class DatasetBuilder(BaseBuilder):
    """
    """

    def __init__(self):
        super().__init__()
        self._dataset_len = None
        self._input_dict = OrderedDict()
        self._output_dict = OrderedDict()
        self._check_delegate_flag = set()
        self._idnames = []

        self._input_transform = OrderedDict()
        self._output_transform = OrderedDict()
        self._global_transform = []

    def __repr__(self):
        return (f"{self.__class__.__name__}("
                f"inputs: {','.join(self._input_dict.keys())}; "
                f"outputs: {','.join(self._output_dict.keys())}; "
                f"size: {len(self)}; "
                f")")

    def __getitem__(self, index):
        index = self._map_index(index)

        names = set(self._output_dict.values())

        raw = {}
        for name in names:  # type:str
            source_ = self._input_dict[name]
            if isinstance(source_, DataDelegate):  # assign name for Delegate Data
                sample_ = source_[index]
                self._check_delegate_res(sample_)
                ress = self._unpack_delegate_res(name, sample_)
                for res in ress:
                    raw[res.name] = res.value
            else:  # simple data source
                sample_ = source_[index]
                raw[name] = sample_

        for name, transform in self._input_transform.items():
            if name in raw:
                raw[name] = transform(raw[name])

        sample = {
            outkey: raw[name] for outkey, name in self._output_dict.items()
        }

        for outkey, transform in self._output_transform.items():
            if outkey in sample:
                sample[outkey] = transform(sample[outkey])

        for transform in self._global_transform:
            sample = transform(sample)

        if self._chain:
            return list(sample.values())
        return sample

    @property
    def raw_len(self):
        if self._dataset_len is None:
            return 0
        return self._dataset_len

    def _unpack_delegate_res(self, delegate_name: str,
                             sample_: Union[Data, List[Data], Dict[str, Data]]) -> List[Data]:
        res = []
        if isinstance(sample_, Data):
            if sample_.name is None:
                sample_.name = delegate_name
            res.append(sample_)
        elif isinstance(sample_, list):
            for i, sample__ in enumerate(sample_):
                res.extend(self._unpack_delegate_res(f"{delegate_name}_{i}", sample__))
        elif isinstance(sample_, dict):
            for k, v in sample_.items():
                if isinstance(v, Data):
                    # name will be assigned by dict key when Data.name is None
                    if v.name is not None:
                        v.name = k
                    res.append(v)
                elif isinstance(v, (tuple, list, dict)):  # untreated for deeper level dict
                    res.append(self._unpack_delegate_res(k, v))
                else:  # unwrapped value in dict is allowed, cause we can build it by dict key.
                    res.append(Data(v, name=k))
        return res

    def _check_delegate_res(self, sample_, delegate_name=None):
        """
        To format sample output data as a dict, the basic element of the delegate returned result
        must be wrapped in `Data`. This function will check if every basic elements in sample is a Data instance.
        If not, a `DelegateDataTypeError` Exception will be raised.
        """
        if delegate_name in self._check_delegate_flag:
            return True

        if isinstance(sample_, Data):
            self._check_delegate_flag.add(delegate_name)
            return True
        elif isinstance(sample_, (list, tuple)):
            for i in sample_:
                self._check_delegate_res(i, delegate_name)
            self._check_delegate_flag.add(delegate_name)
            return True
        elif isinstance(sample_, dict):
            for k, v in sample_.items():
                if isinstance(v, (list, dict, tuple)):
                    self._check_delegate_res(v, delegate_name)
            self._check_delegate_flag.add(delegate_name)
            return True
        else:
            raise DelegateDataTypeError(
                f'Delegate must return `Data` object or its list/dict wrap, but got {type(sample_)}.'
            )

    def _check_source_name(self, name, added=True):
        """
        Each source must have different name, or the newer will replace the older.
        """
        if added:
            assert name not in self._input_dict, f'source "{name}" has been added'
        else:
            assert name in self._input_dict, f'source "{name}" has not been added'

    def _check_outkey_name(self, outkey, added=True):
        """
        Each output key must be different, or new output will cover the old one.
        """
        if added:
            assert outkey not in self._output_dict, f'outkey "{outkey}" has been added'
        else:
            assert outkey in self._output_dict, f'outkey "{outkey}" has not been added'

    def _map_index(self, index):
        """
        Map the raw index to the final index for source data.
        Args:
            index:

        Returns:

        """
        if self._sub_indices is not None:
            index = self._sub_indices[index]
        if self._reindices is not None:
            index = self._reindices[index]

        return index

    def add_ids(self, name):
        assert name not in self._idnames, f'id key "{name}" has been added'
        self._idnames.append(name)
        return self

    def add_input(self, name, source: Union[Sized, DataDelegate]):
        """
        Add an source/input stream.
        Args:
            name:
            source:
        """
        self._check_len(source)
        self._check_source_name(name, added=True)
        self._input_dict[name] = source
        return self

    def add_output(self, name, outkey):
        """
        Add an output stream.
        Args:
            name:
            outkey:
        """
        self._check_source_name(name, added=False)
        self._check_outkey_name(outkey)
        self._output_dict[outkey] = name
        return self

    def add_input_transform(self, name, transform: Callable[[Any], Any]):
        """
        If there are multiple outputs want to fetch data from a same source, and there is a function that
        only need to be called once, then you can add an `input transform` to achieve it.

        Args:
            name:
            transform:

        Notes:
              One input source with name `name` will only have one input transform, an Exception will be raised
              when added second one with the same `name`. So combine your transforms when you have multiple transforms
              for the `name` source.
        """
        self._check_source_name(name, added=False)
        self._input_transform[name] = transform
        return self

    def add_output_transform(self, outkey, transform: Callable[[Any], Any]):
        """
        When you add an `output transform` with `outkey`, it means the output data with name `outkey`
        will be passed into this transform by call `transform(data)`

        Args:
            outkey:
            transform: callable object

        Notes:
              One output data with name `outkey` will only have one transform, an Exception will be raised
              when added second one with the same `outkey`. So combine your transforms when you have multiple transforms
              for the `outkey` data.
        """
        self._check_outkey_name(outkey, added=False)
        self._output_transform[outkey] = transform
        return self

    def add_global_transform(self, transform: Callable[[Dict[str, Any]], Any]):
        self._global_transform.append(transform)
        return self

    def copy(self):
        builder = super().copy()

        builder._dataset_len = self._dataset_len
        builder._input_dict = OrderedDict(self._input_dict)
        builder._output_dict = OrderedDict(self._output_dict)
        builder._idnames = list(self._idnames)

        builder._input_transform = OrderedDict(self._input_transform)
        builder._output_transform = OrderedDict(self._output_transform)
        builder._global_transform = list(self._global_transform)

        return builder

    @property
    def sampler(self):
        return self._sampler

    @property
    def batch_sampler(self):
        return self._batch_sampler


class SimpleDataset(DatasetBuilder):
    def __init__(self,
                 xs=None, ys=None,
                 transform=None, target_transform=None, global_transform=None,
                 indices: Sequence[int] = None, subset: Sequence[int] = None,
                 chain=False):
        super().__init__()
        if xs is not None:
            self.add_input(name='xs', source=xs).add_output(name='xs', outkey='xs')
            if transform is not None:
                self.add_output_transform('xs', transform)

        if ys is not None:
            self.add_input(name='ys', source=ys).add_output(name='ys', outkey='ys')
            if target_transform is not None:
                self.add_output_transform('ys', target_transform)

        if global_transform is not None:
            self.add_global_transform(global_transform)

        if indices is not None:
            self.reindices(indices)

        if subset is not None:
            self.subset(subset)

        if chain:
            self.chain()


class ConcatDataset(BaseBuilder):
    def __init__(self, *builders: DatasetBuilder):
        self._check_builder_iter_type(*builders)
        self._chunk = builders
        super().__init__()

    def _check_builder_iter_type(self, *args: DatasetBuilder):
        assert sum([i._chain for i in args]) in {0, len(args)}
        self._chain = args[0]._chain

    def __repr__(self):
        return (f"{self.__class__.__name__}("
                f"Chains: {','.join([str(i) for i in self._chunk])}; "
                f"Size: {len(self)}; "
                f")")


class ChainDataset(ConcatDataset):

    def __getitem__(self, index):
        index = self._map_index(index)
        lens = [len(i) for i in self._chunk]

        for i, offset in enumerate(accumulate(lens, add)):
            if index < offset:
                return self._chunk[index - lens[i]]

    @property
    def raw_len(self):
        return sum(len(i) for i in self._chunk)


class ZipDataset(ConcatDataset):
    def __init__(self, *builders: BaseBuilder):
        super().__init__(*builders)
        self._counter = [0 for _ in builders]

    def __len__(self):
        return super().__len__()

    @property
    def raw_len(self):
        return max(len(i) for i in self._chunk)

    def __getitem__(self, index):
        index = self._map_index(index)
        lens = [len(i) for i in self._chunk]

        n_counter = []
        ress = []
        for count, builder, len_ in zip(self._counter, self._chunk, lens):
            if index >= len_:
                index = ((index + count) % len_)
                count = (count + 1) % len_
            res = builder[index]
            ress.append(res)
            n_counter.append(count)

        self._counter = n_counter

        if not self._chain:
            nress = {}
            for res in ress:
                for k, v in res.items():
                    nress[k] = v
            ress = nress
        return ress
