import os
from enum import IntEnum
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

from faim_hcs.UIntHistogram import UIntHistogram


class PlateLayout(IntEnum):
    """Plate layout, 96-well or 384-well."""

    I96 = 96
    I384 = 384


def _get_row_cols(layout: Union[PlateLayout, int]) -> tuple[list[str], list[str]]:
    """Return rows and columns for requested layout."""
    if layout == PlateLayout.I96:
        rows = ["A", "B", "C", "D", "E", "F", "G", "H"]
        cols = [str(i) for i in range(1, 13)]
        assert len(rows) * len(cols) == 96
    elif layout == PlateLayout.I384:
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
    root_dir: Path,
    name: str,
    layout: PlateLayout,
    files: pd.DataFrame,
    order_name: str,
    barcode: str,
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

    plate_path = join(root_dir, name + ".zarr")
    os.makedirs(plate_path, exist_ok=False)

    store = parse_url(plate_path, mode="w").store
    plate = zarr.group(store=store)

    write_plate_metadata(
        plate,
        columns=cols,
        rows=rows,
        wells=[f"{w[0]}/{str(int(w[1:]))}" for w in files["well"].unique()],
        name=name,
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
    name: str = None,
    layout: Union[PlateLayout, int] = PlateLayout.I96,
    order_name: str = "order-name",
    barcode: str = "barcode",
) -> Group:
    """Build empty zarr scaffold of a ome-zarr NGFF conform HCS experiment.

    Additionally `order_name` and `barcode` are added to the plate.attrs.

    :param root_dir: where the zarr is stored
    :param files: table of image files
    :param name: Name of the plate-zarr. By default taken from metadata.
    :param layout: plate layout
    :param order_name: plate order name
    :param barcode: plate barcode
    :return: zarr plate group
    """
    names = files["name"].unique()
    assert len(names) == 1, "Files do belong to more than one plate."
    if name is None:
        name = files["name"].unique()[0]

    plate = _create_zarr_plate(
        root_dir=root_dir,
        name=name,
        layout=layout,
        files=files,
        order_name=order_name,
        barcode=barcode,
    )

    _add_wells_to_plate(plate=plate, files=files)

    return plate


def _add_image_metadata(
    img_group: Group,
    ch_metadata: dict,
    dtype: type,
    histograms: list[UIntHistogram],
):
    attrs = img_group.attrs.asdict()

    # Add omero metadata
    attrs["omero"] = build_omero_channel_metadata(ch_metadata, dtype, histograms)

    # Add metaseries metadta
    attrs["acquisition_metadata"] = {"channels": ch_metadata}

    # Save histograms and add paths to attributes
    histogram_paths = []
    for i, (ch, hist) in enumerate(zip(ch_metadata, histograms)):
        ch_name = ch["channel-name"].replace(" ", "_")
        hist_name = f"C{str(i).zfill(2)}_{ch_name}_histogram.npz"
        hist.save(join(img_group.store.path, img_group.path, hist_name))
        histogram_paths.append(hist_name)

    attrs["histograms"] = histogram_paths

    img_group.attrs.put(attrs)


def _compute_chunk_size_cyx(
    img: ArrayLike,
    max_levels: int = 4,
    max_size: int = 2048,
    write_empty_chunks: bool = True,
) -> tuple[list[dict[str, list[int]]], int]:
    """Compute chunk-size for zarr storage.

    :param img: to be saved
    :param max_levels: max resolution pyramid levels
    :param max_size: chunk size maximum
    :return: storage options, number of pyramid levels
    """
    storage_options = []
    chunks = [
        1,
    ] * img.ndim
    for i in range(max_levels + 1):
        h = min(max_size, img.shape[-2] // 2**i)
        w = min(max_size, img.shape[-1] // 2**i)
        chunks[-2] = h
        chunks[-1] = w
        storage_options.append(
            {
                "chunks": chunks.copy(),
                "write_empty_chunks": write_empty_chunks,
            }
        )
        if h <= max_size / 2 and w <= max_size / 2:
            return storage_options, i
    return storage_options, max_levels


def _get_axis_scale(axis, meta):
    if axis["name"] == "x":
        return meta["spatial-calibration-x"]
    if axis["name"] == "y":
        return meta["spatial-calibration-y"]
    if axis["name"] == "z":
        return meta["z-scaling"]
    return 1


def _set_multiscale_metadata(group: Group, general_metadata: dict, axes: list[dict]):
    datasets = group.attrs.asdict()["multiscales"][0]["datasets"]
    scaling = np.array([_get_axis_scale(axis, general_metadata) for axis in axes])

    for ct in datasets:
        rescaled = ct["coordinateTransformations"][0]["scale"] * scaling
        ct["coordinateTransformations"][0]["scale"] = list(rescaled)

    write_multiscales_metadata(group, datasets=datasets, axes=axes)


def write_image_to_group(
    img: ArrayLike,
    axes: list[dict],
    group: Group,
    write_empty_chunks: bool = True,
):
    storage_options, max_layer = _compute_chunk_size_cyx(
        img, write_empty_chunks=write_empty_chunks
    )

    scaler = Scaler(max_layer=max_layer)

    write_image(
        img, group=group, axes=axes, storage_options=storage_options, scaler=scaler
    )


def write_image_and_metadata(
    img: ArrayLike,
    axes: list[dict],
    histograms: list[UIntHistogram],
    ch_metadata: list[dict],
    general_metadata: dict,
    group: Group,
    write_empty_chunks: bool = True,
):
    write_image_to_group(
        img=img,
        axes=axes,
        group=group,
        write_empty_chunks=write_empty_chunks,
    )

    _set_multiscale_metadata(group=group, general_metadata=general_metadata, axes=axes)

    _add_image_metadata(
        img_group=group,
        ch_metadata=ch_metadata,
        dtype=img.dtype,
        histograms=histograms,
    )


def write_cyx_image_to_well(
    img: ArrayLike,
    histograms: list[UIntHistogram],
    ch_metadata: list[dict],
    general_metadata: dict,
    group: Group,
    write_empty_chunks: bool = True,
):
    if general_metadata["spatial-calibration-units"] == "um":
        axes = [
            {"name": "c", "type": "channel"},
            {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"},
        ]
    else:
        raise NotImplementedError("Spatial unit unknown.")

    write_image_and_metadata(
        img=img,
        axes=axes,
        histograms=histograms,
        ch_metadata=ch_metadata,
        general_metadata=general_metadata,
        group=group,
        write_empty_chunks=write_empty_chunks,
    )


def write_czyx_image_to_well(
    img: ArrayLike,
    histograms: list[UIntHistogram],
    ch_metadata: list[dict],
    general_metadata: dict,
    group: Group,
    write_empty_chunks: bool = True,
):
    if general_metadata["spatial-calibration-units"] == "um":
        axes = [
            {"name": "c", "type": "channel"},
            {"name": "z", "type": "space", "unit": "micrometer"},
            {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"},
        ]
    else:
        raise NotImplementedError("Spatial unit unknown.")

    write_image_and_metadata(
        img=img,
        axes=axes,
        histograms=histograms,
        ch_metadata=ch_metadata,
        general_metadata=general_metadata,
        group=group,
        write_empty_chunks=write_empty_chunks,
    )


def build_omero_channel_metadata(
    ch_metadata: dict, dtype: type, histograms: list[UIntHistogram]
):
    """Build omero conform channel metadata to be stored in zarr attributes.

    * Color is computed from the metaseries wavelength metadata.
    * Label is the set to the metaseries _IllumSetting_ metadata.
    * Intensity scaling is obtained from the data histogram [0.01,
    0.99] quantiles.

    :param ch_metadata: channel metadata from tiff-tags
    :param dtype: data type
    :param histograms: histograms of channels
    :return: omero metadata dictionary
    """
    channels = []
    for i, (ch, hist) in enumerate(zip(ch_metadata, histograms)):
        label = ch["channel-name"]
        if "z-projection-method" in ch.keys():
            proj_method = ch["z-projection-method"]
            proj_method = proj_method.replace(" ", "-")
            label = f"{proj_method}-Projection_{label}"

        channels.append(
            {
                "active": True,
                "coefficient": 1,
                "color": ch["display-color"],
                "family": "linear",
                "inverted": False,
                "label": label,
                "wavelength_id": f"C{str(i + 1).zfill(2)}",
                "window": {
                    "min": np.iinfo(dtype).min,
                    "max": np.iinfo(dtype).max,
                    "start": hist.quantile(0.01),
                    "end": hist.quantile(0.99),
                },
            }
        )

    return {"channels": channels}


def _copy_multiscales_metadata(parent_group, subgroup):
    datasets = parent_group.attrs.asdict()["multiscales"][0]["datasets"]
    axes = parent_group.attrs.asdict()["multiscales"][0]["axes"]
    write_multiscales_metadata(subgroup, datasets=datasets, axes=axes)


def write_labels_to_group(
    labels,
    labels_name,
    parent_group: Group,
    write_empty_chunks: bool = True,
    overwrite: bool = False,
):
    try:
        subgroup = parent_group[f"labels/{labels_name}"]
    except KeyError:
        subgroup = parent_group.create_group(
            f"labels/{labels_name}",
            overwrite=overwrite,
        )  # only create group once

    axes = parent_group.attrs.asdict()["multiscales"][0]["axes"]
    assert len(axes) == len(
        labels.shape
    ), f"Group axes don't match label image dimensions: {len(axes)} <> {len(labels.shape)}."

    write_image_to_group(
        img=labels,
        axes=axes,
        group=subgroup,
        write_empty_chunks=write_empty_chunks,
    )

    _copy_multiscales_metadata(parent_group, subgroup)
