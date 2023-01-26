import os
from os.path import join
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd
import zarr
from numpy._typing import ArrayLike
from ome_zarr.io import parse_url
from ome_zarr.scale import Scaler
from ome_zarr.writer import (
    write_image,
    write_multiscales_metadata,
    write_plate_metadata,
    write_well_metadata,
)
from zarr import Group

from faim_hcs.MetaSeriesUtils import build_omero_channel_metadata
from faim_hcs.UIntHistogram import UIntHistogram


def _get_row_cols(layout: str) -> tuple[list[str], list[str]]:
    """Return rows and columns for requested layout."""
    if layout == "96":
        rows = ["A", "B", "C", "D", "E", "F", "G", "H"]
        cols = [str(i) for i in range(1, 13)]
        assert len(rows) * len(cols) == 96
    elif layout == "384":
        rows = [
            "A",
            "B",
            "C",
            "D",
            "E",
            "F",
            "G",
            "H",
            "I",
            "J",
            "K",
            "L",
            "M",
            "N",
            "O",
            "P",
        ]
        cols = [str(i) for i in range(1, 25)]
        assert len(rows) * len(cols) == 384
    else:
        raise NotImplementedError(f"{layout} layout not supported.")

    return rows, cols


def _create_zarr_plate(
    root_dir: Path, layout: str, files: pd.DataFrame, order_name: str, barcode: str
) -> Group:
    """Create plate layout according to ome-zarr NGFF.

    Additionally the `order_name` and `barcode` is added to the plate.attrs.

    :param root_dir: where the zarr is stored
    :param layout: plate layout
    :param files: table of all image files
    :param order_name: plate order name
    :param barcode: plate barcode
    :return: zarr group
    """
    rows, cols = _get_row_cols(layout=layout)

    plate_path = join(root_dir, files["name"].unique()[0] + ".zarr")
    os.makedirs(plate_path, exist_ok=False)

    store = parse_url(plate_path, mode="w").store
    plate = zarr.group(store=store)

    write_plate_metadata(
        plate,
        columns=cols,
        rows=rows,
        wells=[f"{w[0]}/{str(int(w[1:]))}" for w in files["well"].unique()],
        name=files["name"].unique()[0],
        field_count=1,
    )

    attrs = plate.attrs.asdict()
    attrs["order_name"] = order_name
    attrs["barcode"] = barcode
    plate.attrs.put(attrs)

    return plate


def _add_wells_to_plate(plate: Group, files: pd.DataFrame) -> None:
    """Add wells to zarr-plate according to ome-zarr NGFF."""
    for well in files["well"].unique():
        row, col = well[0], str(int(well[1:]))

        if row not in plate:
            plate.create_group(row)

        if col not in plate[row]:
            plate[row].create_group(col).create_group("0")
            write_well_metadata(plate[row][col], [{"path": "0"}])


def build_zarr_scaffold(
    root_dir: Union[str, Path],
    files: pd.DataFrame,
    layout: str = "96",
    order_name: str = "order-name",
    barcode: str = "barcode",
) -> Group:
    """Build empty zarr scaffold of a ome-zarr NGFF conform HCS experiment.

    Additionally `order_name` and `barcode` are added to the plate.attrs.

    :param root_dir: where the zarr is stored
    :param files: table of image files
    :param layout: plate layout
    :param order_name: plate order name
    :param barcode: plate barcode
    :return: zarr plate group
    """
    names = files["name"].unique()
    assert len(names) == 1, "Files do belong to more than one plate."

    plate = _create_zarr_plate(
        root_dir=root_dir,
        layout=layout,
        files=files,
        order_name=order_name,
        barcode=barcode,
    )

    _add_wells_to_plate(plate=plate, files=files)

    return plate


def _add_image_metadata(
    img_group: Group,
    metaseries_ch_metadata: dict,
    dtype: type,
    histograms: list[UIntHistogram],
):
    attrs = img_group.attrs.asdict()

    # Add omero metadata
    attrs["omero"] = build_omero_channel_metadata(
        metaseries_ch_metadata, dtype, histograms
    )

    # Add metaseries metadta
    attrs["metaseries_metadata"] = {"channels": metaseries_ch_metadata}

    # Save histograms and add paths to attributes
    histogram_paths = {}
    for ch, hist in zip(metaseries_ch_metadata, histograms):
        ch_name = ch["_IllumSetting_"].replace(" ", "_")
        hist.save(
            join(img_group.store.path, img_group.path, f"{ch_name}_histogram.npz")
        )
        histogram_paths[ch_name] = f"{ch_name}_histogram.npz"

    attrs["histograms"] = histogram_paths

    img_group.attrs.put(attrs)


def _compute_chunk_size_cyx(
    img: ArrayLike, max_levels: int = 4, max_size: int = 2048
) -> tuple[list[dict[str, list[int]]], int]:
    """Compute chunk-size for zarr storage.

    :param img: to be saved
    :param max_levels: max resolution pyramid levels
    :param max_size: chunk size maximum
    :return: storage options, number of pyramid levels
    """
    storage_options = []
    for i in range(max_levels + 1):
        h = min(max_size, img.shape[1] // 2**i)
        w = min(max_size, img.shape[2] // 2**i)
        storage_options.append({"chunks": [1, h, w]})
        if h <= 1024 and w <= 1024:
            return storage_options, i
    return storage_options, max_levels


def _set_multiscale_metadata(group: Group, general_metadata: dict, axes: list[dict]):
    datasets = group.attrs.asdict()["multiscales"][0]["datasets"]
    scaling = np.array(
        [
            1,
            general_metadata["spatial-calibration-x"],
            general_metadata["spatial-calibration-x"],
        ]
    )

    for ct in datasets:
        rescaled = ct["coordinateTransformations"][0]["scale"] * scaling
        ct["coordinateTransformations"][0]["scale"] = list(rescaled)

    write_multiscales_metadata(group, datasets=datasets, axes=axes)


def write_cyx_image_to_well(
    img: ArrayLike,
    histograms: list[UIntHistogram],
    metaseries_ch_metadata: list[dict],
    general_metadata: dict,
    group: Group,
):
    if general_metadata["spatial-calibration-units"] == "um":
        axes = [
            {"name": "c", "type": "channel"},
            {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"},
        ]
    else:
        NotImplementedError("Spatial unit unknown.")

    storage_options, max_layer = _compute_chunk_size_cyx(img)

    scaler = Scaler(max_layer=max_layer)

    write_image(
        img, group=group, axes=axes, storage_options=storage_options, scaler=scaler
    )

    _set_multiscale_metadata(group=group, general_metadata=general_metadata, axes=axes)

    _add_image_metadata(
        img_group=group,
        metaseries_ch_metadata=metaseries_ch_metadata,
        dtype=img.dtype,
        histograms=histograms,
    )
