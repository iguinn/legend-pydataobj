from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterator, Mapping
from copy import deepcopy
from functools import partial
from multiprocessing.pool import Pool
from typing import Any, Union
from warnings import warn

import awkward as ak
import numpy as np
import pandas as pd
from hist import Hist, axis
from numpy.typing import NDArray

from ..types import Array, Scalar, Struct, VectorOfVectors
from ..units import default_units_registry as ureg
from .store import LH5Store
from .utils import expand_path

LGDO = Union[Array, Scalar, Struct, VectorOfVectors]


class LH5Iterator(Iterator):
    """
    A class for iterating through one or more LH5 files, one block of entries
    at a time. This also accepts an entry list/mask to enable event selection,
    and a field mask.

    This can be used as an iterator:


    >>> for lh5_obj in LH5Iterator(...):
    >>>    # do the thing!

    This is intended for if you are reading a large quantity of data. This
    will ensure that you traverse files efficiently to minimize caching time
    and will limit your memory usage (particularly when reading in waveforms!).
    The ``lh5_obj`` that is read by this class is reused in order to avoid
    reallocation of memory; this means that if you want to hold on to data
    between reads, you will have to copy it somewhere!

    When defining an LH5Iterator, you must give it a list of files and the
    hdf5 groups containing the data tables you are reading. You may also
    provide a field mask, and an entry list or mask, specifying which entries
    to read from the files. You may also pair it with a friend iterator, which
    contains a parallel group of files which will be simultaneously read.
    In addition to accessing requested data via ``lh5_obj``, several
    properties exist to tell you where that data came from:

    - lh5_it.current_i_entry: get the index within the entry list of the
      first entry that is currently read
    - lh5_it.current_local_entries: get the entry numbers relative to the
      file the data came from
    - lh5_it.current_global_entries: get the entry number relative to the
      full dataset
    - lh5_it.current_files: get the file name corresponding to each entry
    - lh5_it.current_groups: get the group name corresponding to each entry

    This class can also be used for random access:

    >>> lh5_obj = lh5_it.read(i_entry)

    to read the block of entries starting at i_entry. In case of multiple files
    or the use of an event selection, i_entry refers to a global event index
    across files and does not count events that are excluded by the selection.
    """

    def __init__(
        self,
        lh5_files: str | Collection[str],
        groups: str | Collection[str] | Collection[Collection[str]],
        base_path: str = "",
        entry_list: Collection[int] | Collection[Collection[int]] | None = None,
        entry_mask: Collection[bool] | Collection[Collection[bool]] | None = None,
        i_start: int = 0,
        n_entries: int | None = None,
        field_mask: Mapping[str, bool] | Collection[str] | None = None,
        buffer_len: int = "100*MB",
        file_cache: int = 10,
        file_map: NDArray[int] = None,
        friend: LH5Iterator | None = None,
    ) -> None:
        """
        Parameters
        ----------
        lh5_files
            file or files to read from. May include wildcards and environment
            variables.
        groups
            HDF5 group(s) to read. If a list of strings is provided, use
            same groups for each file. If a list of lists is provided, size
            of outer list must match size of file list, and each inner list
            will apply to a single file (or set of wildcarded files)
        entry_list
            list of entry numbers to read. If a nested list is provided,
            expect one top-level list for each file, containing a list of
            local entries. If a list of ints is provided, use global entries.
        entry_mask
            mask of entries to read. If a list of arrays is provided, expect
            one for each file. Ignore if a selection list is provided.
        i_start
            index of first entry to start at when iterating
        n_entries
            number of entries to read before terminating iteration
        field_mask
            mask of which fields to read. See :meth:`LH5Store.read` for
            more details.
        buffer_len
            number of entries to read at a time while iterating through files.
        file_cache
            maximum number of files to keep open at a time
        file_map
            cumulative file/group entries. This can be provided on construction
            to speed up random or sparse access; otherwise, we sequentially
            read the size of each group. WARNING: no checks for accuracy are
            performed so only use this if you know what you are doing!
        friend
            a \"friend\" LH5Iterator that will be read in parallel with this.
            The friend should have the same length and entry list. A single
            LH5 table containing columns from both iterators will be returned.
            Note that buffer_len will be set to the minimum of the two.
        """
        self.lh5_st = LH5Store(base_path=base_path, keep_open=file_cache)

        # List of files, with wildcards and env vars expanded
        if isinstance(lh5_files, str):
            lh5_files = [lh5_files]
        elif not isinstance(lh5_files, (Collection)):
            msg = "lh5_files must be a string or list of strings"
            raise ValueError(msg)

        if isinstance(groups, str):
            groups = [[groups]] * len(lh5_files)
        elif not isinstance(groups, Collection):
            msg = "group must be a string or appropriate list"
            raise ValueError(msg)
        elif all(isinstance(g, str) for g in groups):
            groups = [groups] * len(lh5_files)
        elif len(groups) == len(lh5_files) and all(
            isinstance(gr_list, (list, tuple, set)) for gr_list in groups
        ):
            pass
        else:
            msg = "group must be a string or appropriate list"
            raise ValueError(msg)

        if len(groups) != len(lh5_files):
            msg = "lh5_files and groups must have same length"
            raise ValueError(msg)

        # make flattened outer-product-like list of files and groups
        self.lh5_files = []
        self.groups = []
        for f, g in zip(lh5_files, groups):
            for f_exp in expand_path(f, list=True, base_path=base_path):
                self.lh5_files += [f_exp] * len(g)
                self.groups += list(g)

        if entry_list is not None and entry_mask is not None:
            msg = "entry_list and entry_mask arguments are mutually exclusive"
            raise ValueError(msg)

        # Map to last row in each file
        if file_map is None:
            self.file_map = np.full(len(self.lh5_files), np.iinfo("q").max, "q")
        else:
            self.file_map = np.array(file_map)

        # Map to last iterator entry for each file
        self.entry_map = np.full(len(self.lh5_files), np.iinfo("q").max, "q")
        self.buffer_len = buffer_len

        if len(self.lh5_files) > 0:
            f = self.lh5_files[0]
            g = self.groups[0]
            n_rows = self.lh5_st.read_n_rows(g, f)

            if isinstance(self.buffer_len, str):
                self.buffer_len = ureg.Quantity(buffer_len)
            if isinstance(self.buffer_len, ureg.Quantity):
                self.buffer_len = int(
                    self.buffer_len
                    / (self.lh5_st.read_size_in_bytes(g, f) * ureg.B)
                    * n_rows
                )

            self.lh5_buffer = self.lh5_st.get_buffer(
                g,
                f,
                size=self.buffer_len,
                field_mask=field_mask,
            )
            if file_map is None:
                self.file_map[0] = n_rows
        else:
            msg = f"can't open any files from {lh5_files}"
            raise RuntimeError(msg)

        self.i_start = i_start
        self.n_entries = n_entries
        self.current_i_entry = 0
        self.next_i_entry = 0

        self.field_mask = field_mask

        # List of entry indices from each file
        self.local_entry_list = None
        self.global_entry_list = None
        if entry_list is not None:
            entry_list = list(entry_list)
            if isinstance(entry_list[0], int):
                self.local_entry_list = [None] * len(self.file_map)
                self.global_entry_list = np.array(entry_list, "q")
                self.global_entry_list.sort()

            else:
                self.local_entry_list = [[]] * len(self.file_map)
                for i_file, local_list in enumerate(entry_list):
                    self.local_entry_list[i_file] = np.array(local_list, "q")
                    self.local_entry_list[i_file].sort()

        elif entry_mask is not None:
            # Convert entry mask into an entry list
            if isinstance(entry_mask, pd.Series):
                entry_mask = entry_mask.to_numpy()
            if isinstance(entry_mask, np.ndarray):
                self.local_entry_list = [None] * len(self.file_map)
                self.global_entry_list = np.nonzero(entry_mask)[0]
            else:
                self.local_entry_list = [[]] * len(self.file_map)
                for i_file, local_mask in enumerate(entry_mask):
                    self.local_entry_list[i_file] = np.nonzero(local_mask)[0]

        # Attach the friend
        self.friend = None
        if friend is not None:
            self.add_friend(friend)

    def add_friend(self, friend: LH5Iterator):
        if not isinstance(friend, LH5Iterator):
            msg = "Friend must be an iterator"
            raise ValueError(msg)

        # set buffer_lens to be equal
        if self.buffer_len < friend.buffer_len:
            friend.buffer_len = self.buffer_len
            friend.lh5_buffer.resize(self.buffer_len)
        elif self.buffer_len > friend.buffer_len:
            self.buffer_len = friend.buffer_len
            self.lh5_buffer.resize(friend.buffer_len)

        # add friend
        self.lh5_buffer.join(friend.lh5_buffer)
        if self.friend is None:
            self.friend = friend
        else:
            self.friend.add_friend(friend)

    def _get_file_cumlen(self, i_file: int) -> int:
        """Helper to get cumulative file length of file"""
        if i_file < 0:
            return 0
        fcl = self.file_map[i_file]

        # if we haven't already calculated, calculate for all files up to i_file
        if fcl == np.iinfo("q").max:
            i_start = np.searchsorted(self.file_map, np.iinfo("q").max)
            fcl = self.file_map[i_start - 1] if i_start > 0 else 0

            for i in range(i_start, i_file + 1):
                fcl += self.lh5_st.read_n_rows(self.groups[i], self.lh5_files[i])
                self.file_map[i] = fcl
        return fcl

    @property
    def current_entry(self) -> int:
        "deprecated alias for current_i_entry"
        warn(
            "current_entry has been renamed to current_i_entry.",
            DeprecationWarning,
            stacklevel=2,
        )

        return self.current_i_entry

    def _get_file_cumentries(self, i_file: int) -> int:
        """Helper to get cumulative iterator entries in file"""
        if i_file < 0:
            return 0
        n = self.entry_map[i_file]

        # if we haven't already calculated, calculate for all files up to i_file
        if n == np.iinfo("q").max:
            i_start = np.searchsorted(self.entry_map, np.iinfo("q").max)
            n = self.entry_map[i_start - 1] if i_start > 0 else 0

            for i in range(i_start, i_file + 1):
                elist = self.get_file_entrylist(i)
                fcl = self._get_file_cumlen(i)
                if elist is None:
                    # no entry list provided
                    n = fcl
                else:
                    n += len(elist)
                    # check that file entries fall inside of file
                    if len(elist) > 0 and elist[-1] >= fcl:
                        logging.warning(f"Found entries out of range for file {i}")
                        n += np.searchsorted(elist, fcl, "right") - len(elist)
                self.entry_map[i] = n
        return n

    def get_file_entrylist(self, i_file: int) -> np.ndarray:
        """Helper to get entry list for file"""
        # If no entry list is provided
        if self.local_entry_list is None:
            return None

        elist = self.local_entry_list[i_file]
        if elist is None:
            # Get local entrylist for this file from global entry list
            f_start = self._get_file_cumlen(i_file - 1)
            f_end = self._get_file_cumlen(i_file)
            i_start = self._get_file_cumentries(i_file - 1)
            i_stop = np.searchsorted(self.global_entry_list, f_end, "right")
            elist = np.array(self.global_entry_list[i_start:i_stop], "q") - f_start
            self.local_entry_list[i_file] = elist
        return elist

    def get_global_entrylist(self) -> np.ndarray:
        """Get global entry list, constructing it if needed"""
        if self.global_entry_list is None and self.local_entry_list is not None:
            self.global_entry_list = np.zeros(len(self), "q")
            for i_file in range(len(self.lh5_files)):
                i_start = self._get_file_cumentries(i_file - 1)
                i_stop = self._get_file_cumentries(i_file)
                f_start = self._get_file_cumlen(i_file - 1)
                self.global_entry_list[i_start:i_stop] = (
                    self.get_file_entrylist(i_file) + f_start
                )
        return self.global_entry_list

    def read(self, i_entry: int, n_entries: int | None = None) -> LGDO:
        "Read the nextlocal chunk of events, starting at entry."
        self.lh5_buffer.resize(0)

        if n_entries is None:
            n_entries = self.buffer_len
        elif n_entries == 0:
            return self.lh5_buffer
        elif n_entries > self.buffer_len:
            msg = "n_entries cannot be larger than buffer_len"
            raise ValueError(msg)

        # if file hasn't been opened yet, search through files
        # sequentially until we find the right one
        i_file = np.searchsorted(self.entry_map, i_entry, "right")
        if i_file < len(self.lh5_files) and self.entry_map[i_file] == np.iinfo("q").max:
            while i_file < len(self.lh5_files) and i_entry >= self._get_file_cumentries(
                i_file
            ):
                i_file += 1

        if i_file == len(self.lh5_files):
            return self.lh5_buffer
        local_i_entry = i_entry - self._get_file_cumentries(i_file - 1)

        while len(self.lh5_buffer) < n_entries and i_file < len(self.file_map):
            # Loop through files
            local_idx = self.get_file_entrylist(i_file)
            if local_idx is not None and len(local_idx) == 0:
                i_file += 1
                local_i_entry = 0
                continue

            i_local = local_i_entry if local_idx is None else local_idx[local_i_entry]
            self.lh5_buffer = self.lh5_st.read(
                self.groups[i_file],
                self.lh5_files[i_file],
                start_row=i_local,
                n_rows=n_entries - len(self.lh5_buffer),
                idx=local_idx,
                field_mask=self.field_mask,
                obj_buf=self.lh5_buffer,
                obj_buf_start=len(self.lh5_buffer),
            )

            i_file += 1
            local_i_entry = 0

        self.current_i_entry = i_entry

        if self.friend is not None:
            self.friend.read(i_entry)

        return self.lh5_buffer

    def reset_field_mask(self, mask):
        """Replaces the field mask of this iterator and any friends with mask"""
        self.field_mask = mask
        if self.friend is not None:
            self.friend.reset_field_mask(mask)

    @property
    def current_local_entries(self) -> NDArray[int]:
        """Return list of local file entries in buffer"""
        cur_entries = np.zeros(len(self.lh5_buffer), dtype="int32")
        i_file = np.searchsorted(self.entry_map, self.current_i_entry, "right")
        file_start = self._get_file_cumentries(i_file - 1)
        i_local = self.current_i_entry - file_start
        i = 0

        while i < len(cur_entries):
            # number of entries to read from this file
            file_end = self._get_file_cumentries(i_file)
            n = min(file_end - file_start - i_local, len(cur_entries) - i)
            entries = self.get_file_entrylist(i_file)

            if entries is None:
                cur_entries[i : i + n] = np.arange(i_local, i_local + n)
            else:
                cur_entries[i : i + n] = entries[i_local : i_local + n]

            i_file += 1
            file_start = file_end
            i_local = 0
            i += n

        return cur_entries

    @property
    def current_global_entries(self) -> NDArray[int]:
        """Return list of local file entries in buffer"""
        cur_entries = np.zeros(len(self.lh5_buffer), dtype="int32")
        i_file = np.searchsorted(self.entry_map, self.current_i_entry, "right")
        file_start = self._get_file_cumentries(i_file - 1)
        i_local = self.current_i_entry - file_start
        i = 0

        while i < len(cur_entries):
            # number of entries to read from this file
            file_end = self._get_file_cumentries(i_file)
            n = min(file_end - file_start - i_local, len(cur_entries) - i)
            entries = self.get_file_entrylist(i_file)

            if entries is None:
                cur_entries[i : i + n] = self._get_file_cumlen(i_file - 1) + np.arange(
                    i_local, i_local + n
                )
            else:
                cur_entries[i : i + n] = (
                    self._get_file_cumlen(i_file - 1) + entries[i_local : i_local + n]
                )

            i_file += 1
            file_start = file_end
            i_local = 0
            i += n

        return cur_entries

    @property
    def current_files(self) -> NDArray[str]:
        """Return list of file names for entries in buffer"""
        cur_files = np.zeros(len(self.lh5_buffer), dtype=object)
        i_file = np.searchsorted(self.entry_map, self.current_i_entry, "right")
        file_start = self._get_file_cumentries(i_file - 1)
        i_local = self.current_i_entry - file_start
        i = 0

        while i < len(cur_files):
            # number of entries to read from this file
            file_end = self._get_file_cumentries(i_file)
            n = min(file_end - file_start - i_local, len(cur_files) - i)
            cur_files[i : i + n] = self.lh5_files[i_file]

            i_file += 1
            file_start = file_end
            i_local = 0
            i += n

        return cur_files

    @property
    def current_groups(self) -> NDArray[str]:
        """Return list of group names for entries in buffer"""
        cur_groups = np.zeros(len(self.lh5_buffer), dtype=object)
        i_file = np.searchsorted(self.entry_map, self.current_i_entry, "right")
        file_start = self._get_file_cumentries(i_file - 1)
        i_local = self.current_i_entry - file_start
        i = 0

        while i < len(cur_groups):
            # number of entries to read from this file
            file_end = self._get_file_cumentries(i_file)
            n = min(file_end - file_start - i_local, len(cur_groups) - i)
            cur_groups[i : i + n] = self.groups[i_file]

            i_file += 1
            file_start = file_end
            i_local = 0
            i += n

        return cur_groups

    def __len__(self) -> int:
        """Return the total number of entries."""
        return (
            self._get_file_cumentries(len(self.lh5_files) - 1)
            if len(self.entry_map) > 0
            else 0
        )

    def __iter__(self) -> LH5Iterator:
        """Loop through entries in blocks of size buffer_len."""
        self.current_i_entry = 0
        self.next_i_entry = self.i_start
        return self

    def __next__(self) -> tuple[LGDO, int, int]:
        """Read next buffer_len entries and return lh5_table and iterator entry."""
        n_entries = self.n_entries
        if n_entries is not None:
            n_entries = min(
                self.buffer_len, n_entries + self.i_start - self.next_i_entry
            )

        buf = self.read(self.next_i_entry, n_entries)
        if len(buf) == 0:
            raise StopIteration
        self.next_i_entry = self.current_i_entry + len(buf)
        return buf

    def __deepcopy__(self, memo):
        """Deep copy everything except lh5_st and friend"""
        result = LH5Iterator.__new__(LH5Iterator)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k == "lh5_st":
                result.lh5_st = LH5Store(
                    base_path=self.lh5_st.base_path, keep_open=self.lh5_st.keep_open
                )
            elif k == "friend":
                result.friend = None
            else:
                setattr(result, k, deepcopy(v, memo))
        return result

    def __getstate__(self):
        """Deep copy lh5_buf when unpickling to avoid weird ownership issues"""
        return dict(
            self.__dict__,
            lh5_st={
                "base_path": self.lh5_st.base_path,
                "keep_open": self.lh5_st.keep_open,
            },
            lh5_buffer=None,
        )

    def __setstate__(self, d):
        """Reinitialize lh5_st and lh5_buffer to avoid potential issues"""
        self.__dict__ = d
        self.lh5_st = LH5Store(**(d["lh5_st"]))
        self.lh5_st.gimme_file(self.lh5_files[0])
        self.lh5_buffer = self.lh5_st.get_buffer(
            self.groups[0],
            self.lh5_files[0],
            size=self.buffer_len,
            field_mask=self.field_mask,
        )
        if self.friend is not None:
            self.lh5_buffer.join(self.friend.lh5_buffer)

    def _generate_workers(self, n_workers: int):
        """Create n_workers copy of this iterator, dividing the files and
        groups between them. These are intended for parallel use"""
        if self.friend is not None:
            friend_its = self.friend._generate_workers(n_workers)

        i_files = np.linspace(0, len(self.lh5_files), n_workers + 1).astype("int")
        # if we have an entry list, get local entries for all files
        if self.local_entry_list is not None:
            local_entry_list = [
                self.get_file_entries(i) for i in range(len(self.lh5_files))
            ]

        worker_its = []
        for i_worker in range(n_workers):
            it = deepcopy(self)

            # worker should only include subset of files
            s = slice(i_files[i_worker], i_files[i_worker + 1])
            it.lh5_files = it.lh5_files[s]
            it.groups = it.groups[s]
            it.file_map = it.file_map[s]
            it.entry_map = it.entry_map[s]
            if i_files[i_worker] - 1 > 0:
                np.subtract(
                    it.file_map,
                    self.file_map[i_files[i_worker] - 1],
                    out=it.file_map,
                    where=it.file_map != np.iinfo("q").max,
                )
                np.subtract(
                    it.entry_map,
                    self.entry_map[i_files[i_worker] - 1],
                    out=it.entry_map,
                    where=it.entry_map != np.iinfo("q").max,
                )

            if it.local_entry_list is not None:
                it.local_entry_list = local_entry_list[s]
            it.global_entry_list = None

            if self.friend is not None:
                it.add_friend(friend_its[i_worker])

            worker_its += [it]

        return worker_its

    def map(
        self, fun: Callable[LH5Iterator, int, int], processes: Pool | int = None
    ) -> list(Any):
        """Map function over iterator blocks and return order-preserving list
        of outputs. Can be multi-threaded provided there are no attempts
        to modify existing objects.

        Parameters
        ----------
        fun:
            function with signature fun(lh5_obj: LGDO, it: LH5Iterator) -> Any
            Outputs of function will be collected in list and returned
        processes:
            number of processes or multiprocessing processor pool
        """
        if processes is None:
            return _map_helper(fun, self)
        if isinstance(processes, int):
            processes = Pool(processes)
        it_pool = self._generate_workers(processes._processes)
        result = processes.map(partial(_map_helper, fun), it_pool)

        return [r for res in result for r in res]

    def accumulate(
        self,
        fun: Callable[LH5Iterator, int, int],
        processes: Pool | int = None,
        operator: Callable = None,
        init: Any = None,
        merge: Callable = None,
    ) -> Any:
        """Accumulate function output over iterator.

        Parameters
        ----------
        fun:
            function with signature fun(lh5_obj: LGDO, it: LH5Iterator) -> Any
            Outputs of function will be summed together using accumulator function
        processes:
            number of processes or multiprocessing processor pool
        operator:
            function with signature `operator(accumulator: Any, addend: Any) -> Any | None`
            that adds the `addend` (i.e. the output of `fun`) to the `accumulator` (i.e. the
            running total). This will can be in place on the accumulator, or returning the
            next value sum. If `None`, we will simply call `accumulator+=addend`
        init:
            initial value for accumulator. If `None` initialize with first result of `fun`
        merge:
            function to use to combine results from different threads. If `None`, use `operator`
        """
        if processes is None:
            return _accumulate_helper(fun, operator, init, self)
        if isinstance(processes, int):
            processes = Pool(processes)
        it_pool = self._generate_workers(processes._processes)
        results = processes.map(
            partial(_accumulate_helper, fun, operator, init), it_pool
        )

        # merge the results
        accumulator = results.pop(0)
        if merge is None:
            merge = operator
        for addend in results:
            if merge is not None:
                res = merge(accumulator, addend)
                if res is not None:
                    accumulator = res
            else:
                accumulator += addend

        return accumulator

    def query(
        self,
        filter: Callable | str,
        processes: Pool | int = None,
    ):
        """
        Query the data files in the iterator and return the selected data
        as a single table in one of several formats.

        Parameters
        ----------
        filter:
            A filter function for reducing the data files. Can be:
            - A function that returns reduced data, with signature
              fun(lh5_obj: LGDO, it: LH5Iterator). Can return:
              - NDArray: if 1D list of values; if 2D list of lists of values in
                same order as axes
              - Collection[ArrayLike]: return list of values in same order as axes
              - Mapping[str, ArrayLike]: mapping from axis name to values
              - pandas.DataFrame: pandas dataframe. Treat as mapping from column
                name to values
            - A string expression. This will call `pd.DataFrame.query <https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.query.html>`_ and return
              a pandas DataFrame containing all columns in the fields mask.
        processes:
            number of processes or multiprocessing processor pool
        """
        if filter is None:
            return self.accumulate(_identity, processes, self.lh5_buffer.append)
        if isinstance(filter, str):
            return pd.concat(self.map(_pandas_query(filter), processes))
        if isinstance(filter, Callable):
            test = filter(self.lh5_buffer, self)
            if isinstance(test, LGDO):
                return self.accumulate(filter, processes, test.append)
            if isinstance(test, pd.DataFrame):
                return pd.concat(self.map(filter, processes))
            if isinstance(test, np.ndarray):
                return np.concatenate(self.map(filter, processes))
            if isinstance(test, ak.Array):
                return ak.concatenate(self.map(filter, processes))
            msg = f"Cannot call query with return type {test.__class__}. "
            "Allowed return types: LGDO, np.array, pd.DataFrame, ak.Array"
            raise ValueError(msg)
        msg = "filter must be a string or a callable returning types "
        "LGDO, np.array, pd.DataFrame, ak.Array"
        raise ValueError(msg)

    def hist(
        self,
        ax: Hist | axis | Collection[axis],
        filter: Callable | str = None,
        processes: Pool | int = None,
        keys: Collection[str] | str = None,
        **hist_kwargs,
    ) -> Hist:
        """
        Fill a histogram from data produced by our `filter`. If
        `filter` is `None`, fill with all data fetched by iterator.

        Parameters
        ----------
        ax:
            Axis object(s) used to construct the histogram. Can provide a Hist
            which will be filled as well.
        filter:
            string containing a pandas style query function with signature fun(lh5_obj: LGDO, it: LH5Iterator) that
            returns values to be filled into histogram. If `None` return all
            values from `field_mask`. Return types can be:

            - NDArray: if 1D list of values; if 2D list of lists of values in
              same order as axes
            - Collection[ArrayLike]: return list of values in same order as axes
            - Mapping[str, ArrayLike]: mapping from axis name to values
            - pandas.DataFrame: pandas dataframe. Treat as mapping from column
              name to values
        processes:
            number of processes or multiprocessing processor pool
        keys:
            list of keys fields corresponding to axes. Use if filter
            returns a mapping with names different from axis names.
        hist_kwargs:
            additional keyword arguments for constructing Hist. See `hist.Hist`.
        """

        # get initial hist for each thread
        if isinstance(ax, axis.AxesMixin):
            h = Hist(ax, **hist_kwargs)
        elif isinstance(ax, Collection):
            h = Hist(*ax, **hist_kwargs)
        elif isinstance(ax, Hist):
            h = ax.copy()
            h[...] = 0

        if filter is None:
            filter = _identity
        elif isinstance(filter, str):
            filter = _pandas_query(filter)

        h = self.accumulate(
            filter if filter else _identity,
            processes,
            _hist_filler(keys),
            h,
            Hist.__add__,
        )

        if isinstance(ax, Hist):
            ax += h
            return ax
        return h


class _hist_filler:
    def __init__(self, keys):
        if keys is not None:
            if isinstance(keys, str):
                keys = [keys]
            elif not isinstance(keys, list):
                keys = list(keys)
        self.keys = keys

    def __call__(self, hist, data):
        if isinstance(data, np.ndarray) and len(data.shape) == 1:
            hist.fill(data)
        elif isinstance(data, np.ndarray) and len(data.shape) == 2:
            hist.fill(*data)
        elif isinstance(data, (pd.DataFrame, Mapping)):
            if self.keys is not None:
                hist.fill(*[data[k] for k in self.keys])
            else:
                hist.fill(**data)
        elif isinstance(data, ak.Array):
            # TODO: handle nested ak arrays
            if self.keys is not None:
                hist.fill(*[data[k] for k in self.keys])
            else:
                hist.fill(*[data[f] for f in data.fields])
        elif isinstance(data, Collection):
            hist.fill(*data)
        else:
            msg = "data returned by filter is not compatible with hist. Must be a 1d or 2d numpy array, a list of arrays, or a mapping from str to array"
            raise ValueError(msg)


def _identity(val):
    return val


def _map_helper(fun, it):
    return [fun(tab, it) for tab in it]


def _accumulate_helper(fun, op, init, it):
    accumulator = init
    for tab in it:
        addend = fun(tab, it)

        if accumulator is None:
            # if no init, initialize on first entry
            accumulator = addend
        elif op is not None:
            res = op(accumulator, addend)
            if res is not None:
                accumulator = res
        else:
            accumulator += addend

    return accumulator


class _pandas_query:
    "Helper for when query is called on a string"

    def __init__(self, expr):
        self.expr = expr

    def __call__(self, tab, _):
        return tab.view_as("pd").query(self.expr)
