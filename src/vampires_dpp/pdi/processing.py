import itertools
import warnings
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import tqdm.auto as tqdm
from astropy.io import fits
from astropy.time import Time
from numpy.typing import NDArray

from vampires_dpp.image_processing import combine_frames_headers, derotate_cube, derotate_frame
from vampires_dpp.paths import any_file_newer
from vampires_dpp.util import create_or_append, load_fits
from vampires_dpp.wcs import apply_wcs

from .utils import instpol_correct, measure_instpol, measure_instpol_ann, write_stokes_products


def polarization_calibration_triplediff(filenames: Sequence[str]):
    """Return a Stokes cube using the *bona fide* triple differential method. This method will split the input data into sets of 16 frames- 2 for each camera, 2 for each FLC state, and 4 for each HWP angle.

    .. admonition:: Pupil-tracking mode
        :class: tip

        For each of these 16 image sets, it is important to consider the apparant sky rotation when in pupil-tracking mode (which is the default for most VAMPIRES observations). With this naive triple-differential subtraction, if there is significant sky motion, the output Stokes frame will be smeared.

        The parallactic angles for each set of 16 frames should be averaged (``average_angle``) and stored to construct the final derotation angle vector

    Parameters
    ----------
    filenames : Sequence[str]
        list of input filenames to construct Stokes frames from

    Raises
    ------
    ValueError:
        If the input filenames are not a clean multiple of 16. To ensure you have proper 16 frame sets, use ``pol_inds`` with a sorted observation table.

    Returns
    -------
    NDArray
        (4, t, y, x) Stokes cube from all 16 frame sets.
    """
    if len(filenames) % 16 != 0:
        msg = "Cannot do triple-differential calibration without exact sets of 16 frames for each HWP cycle"
        raise ValueError(msg)
    # now do triple-differential calibration
    cube_dict = {}
    cube_errs = []
    headers = {}
    for file in filenames:
        with fits.open(file) as hdul:
            cube = hdul[0].data
            nfields = cube.shape[0]
            prim_hdr = hdul[0].header
            create_or_append(headers, "PRIMARY", prim_hdr)
            cube_err = []
            for hdu in hdul[1 : nfields + 1]:
                hdr = hdu.header
                create_or_append(headers, hdr["FIELD"], hdr)
                cube_err.append(hdul[f"{hdr['FIELD']}ERR"].data)
        # derotate frame - necessary for crosstalk correction
        cube_derot = derotate_cube(cube, prim_hdr["DEROTANG"])
        cube_err_derot = derotate_cube(np.array(cube_err), prim_hdr["DEROTANG"])
        # store into dictionaries
        key = prim_hdr["RET-ANG1"], prim_hdr["U_FLC"], prim_hdr["U_CAMERA"]
        cube_dict[key] = cube_derot
        cube_errs.append(cube_err_derot)

    stokes_cube = triple_diff_dict(cube_dict)
    # swap stokes and field axes so field is first
    stokes_cube = np.swapaxes(stokes_cube, 0, 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stokes_err = np.sqrt(np.nanmean(np.power(cube_errs, 2), axis=0) / len(cube_errs))

    stokes_hdrs = {}
    for key, hdrs in headers.items():
        stokes_hdr = combine_frames_headers(hdrs)
        stokes_hdr = apply_wcs(stokes_hdr, angle=0)
        # reduce exptime by 2 because cam1 and cam2 are simultaneous
        if "TINT" in stokes_hdr:
            stokes_hdr["TINT"] /= 2
        stokes_hdrs[key] = stokes_hdr

    # reform hdulist
    prim_hdu = fits.PrimaryHDU(stokes_cube, stokes_hdrs.pop("PRIMARY"))
    hdul = fits.HDUList(prim_hdu)
    for frame, hdr in zip(stokes_cube, stokes_hdrs.values(), strict=True):
        hdu = fits.ImageHDU(frame, header=hdr, name=hdr["FIELD"])
        hdul.append(hdu)
    for frame_err, hdr in zip(stokes_err, stokes_hdrs.values(), strict=True):
        hdu = fits.ImageHDU(frame_err, header=hdr, name=f"{hdr['FIELD']}ERR")
        hdul.append(hdu)
    return hdul


def polarization_calibration_doublediff(filenames: Sequence[str]):
    if len(filenames) % 8 != 0:
        msg = "Cannot do double-differential calibration without exact sets of 8 frames for each HWP cycle"
        raise ValueError(msg)
    # now do double-differential calibration
    cube_dict = {}
    cube_errs = []
    headers = {}
    for file in filenames:
        with fits.open(file) as hdul:
            cube = hdul[0].data
            nfields = cube.shape[0]
            prim_hdr = hdul[0].header
            create_or_append(headers, "PRIMARY", prim_hdr)
            cube_err = []
            for hdu in hdul[1 : nfields + 1]:
                hdr = hdu.header
                create_or_append(headers, hdr["FIELD"], hdr)
                cube_err.append(hdul[f"{hdr['FIELD']}ERR"].data)
        # derotate frame - necessary for crosstalk correction
        cube_derot = derotate_cube(cube, prim_hdr["DEROTANG"])
        cube_err_derot = derotate_cube(cube_err, prim_hdr["DEROTANG"])
        # store into dictionaries
        key = prim_hdr["RET-ANG1"], prim_hdr["U_CAMERA"]
        cube_dict[key] = cube_derot
        cube_errs.append(cube_err_derot)

    I, Q, U = double_diff_dict(cube_dict)  # noqa: E741
    # swap stokes and field axes so field is first
    stokes_cube = np.swapaxes((I, Q, U), 0, 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stokes_err = np.sqrt(np.nanmean(np.power(cube_errs, 2), axis=0) / len(cube_errs))

    stokes_hdrs = {}
    for key, hdrs in headers.items():
        stokes_hdr = apply_wcs(combine_frames_headers(hdrs), angle=0)
        # reduce exptime by 2 because cam1 and cam2 are simultaneous
        if "TINT" in stokes_hdr:
            stokes_hdr["TINT"] /= 2
        stokes_hdrs[key] = stokes_hdr

    # reform hdulist
    prim_hdu = fits.PrimaryHDU(stokes_cube, stokes_hdrs.pop("PRIMARY"))
    hdul = fits.HDUList(prim_hdu)
    for frame, hdr in zip(stokes_cube, stokes_hdrs.values(), strict=True):
        hdu = fits.ImageHDU(frame, header=hdr, name=hdr["FIELD"])
        hdul.append(hdu)
    for frame_err, hdr in zip(stokes_err, stokes_hdrs.values(), strict=True):
        hdu = fits.ImageHDU(frame_err, header=hdr, name=f"{hdr['FIELD']}ERR")
        hdul.append(hdu)
    return hdul


def make_triplediff_dict(filenames):
    output = {}
    for file in filenames:
        data, hdr = load_fits(file, header=True)
        # get row vector for mueller matrix of each file
        # store into dictionaries
        key = hdr["RET-ANG1"], hdr["U_FLC"], hdr["U_CAMERA"]
        output[key] = data
    return output


def make_doublediff_dict(filenames):
    output = {}
    for file in filenames:
        data, hdr = load_fits(file, header=True)
        # get row vector for mueller matrix of each file
        # store into dictionaries
        key = hdr["RET-ANG1"], hdr["U_CAMERA"]
        output[key] = data
    return output


def triple_diff_dict(input_dict):
    ## make difference images
    # single diff (cams)
    pQ0 = 0.5 * (input_dict[(0, "A", 1)] - input_dict[(0, "A", 2)])
    pIQ0 = 0.5 * (input_dict[(0, "A", 1)] + input_dict[(0, "A", 2)])
    pQ1 = 0.5 * (input_dict[(0, "B", 1)] - input_dict[(0, "B", 2)])
    pIQ1 = 0.5 * (input_dict[(0, "B", 1)] + input_dict[(0, "B", 2)])

    mQ0 = 0.5 * (input_dict[(45, "A", 1)] - input_dict[(45, "A", 2)])
    mIQ0 = 0.5 * (input_dict[(45, "A", 1)] + input_dict[(45, "A", 2)])
    mQ1 = 0.5 * (input_dict[(45, "B", 1)] - input_dict[(45, "B", 2)])
    mIQ1 = 0.5 * (input_dict[(45, "B", 1)] + input_dict[(45, "B", 2)])

    pU0 = 0.5 * (input_dict[(22.5, "A", 1)] - input_dict[(22.5, "A", 2)])
    pIU0 = 0.5 * (input_dict[(22.5, "A", 1)] + input_dict[(22.5, "A", 2)])
    pU1 = 0.5 * (input_dict[(22.5, "B", 1)] - input_dict[(22.5, "B", 2)])
    pIU1 = 0.5 * (input_dict[(22.5, "B", 1)] + input_dict[(22.5, "B", 2)])

    mU0 = 0.5 * (input_dict[(67.5, "A", 1)] - input_dict[(67.5, "A", 2)])
    mIU0 = 0.5 * (input_dict[(67.5, "A", 1)] + input_dict[(67.5, "A", 2)])
    mU1 = 0.5 * (input_dict[(67.5, "B", 1)] - input_dict[(67.5, "B", 2)])
    mIU1 = 0.5 * (input_dict[(67.5, "B", 1)] + input_dict[(67.5, "B", 2)])

    # double difference (FLC1 - FLC2)
    pQ = 0.5 * (pQ0 - pQ1)
    pIQ = 0.5 * (pIQ0 + pIQ1)

    mQ = 0.5 * (mQ0 - mQ1)
    mIQ = 0.5 * (mIQ0 + mIQ1)

    pU = 0.5 * (pU0 - pU1)
    pIU = 0.5 * (pIU0 + pIU1)

    mU = 0.5 * (mU0 - mU1)
    mIU = 0.5 * (mIU0 + mIU1)

    # triple difference (HWP1 - HWP2)
    Q = 0.5 * (pQ - mQ)
    IQ = 0.5 * (pIQ + mIQ)
    U = 0.5 * (pU - mU)
    IU = 0.5 * (pIU + mIU)
    I = 0.5 * (IQ + IU)  # noqa: E741

    return I, Q, U


def double_diff_dict(input_dict):
    ## make difference images
    # single diff (cams)
    pQ = 0.5 * (input_dict[(0, 1)] - input_dict[(0, 2)])
    pIQ = 0.5 * (input_dict[(0, 1)] + input_dict[(0, 2)])

    mQ = 0.5 * (input_dict[(45, 1)] - input_dict[(45, 2)])
    mIQ = 0.5 * (input_dict[(45, 1)] + input_dict[(45, 2)])

    pU = 0.5 * (input_dict[(22.5, 1)] - input_dict[(22.5, 2)])
    pIU = 0.5 * (input_dict[(22.5, 1)] + input_dict[(22.5, 2)])

    mU = 0.5 * (input_dict[(67.5, 1)] - input_dict[(67.5, 2)])
    mIU = 0.5 * (input_dict[(67.5, 1)] + input_dict[(67.5, 2)])

    # double difference (HWP1 - HWP2)
    Q = 0.5 * (pQ - mQ)
    IQ = 0.5 * (pIQ + mIQ)
    U = 0.5 * (pU - mU)
    IU = 0.5 * (pIU + mIU)
    I = 0.5 * (IQ + IU)  # noqa: E741

    return I, Q, U


def mueller_matrix_calibration(mueller_matrices: NDArray, cube: NDArray) -> NDArray:
    stokes_cube = np.zeros_like(cube, shape=(mueller_matrices.shape[-1], *cube.shape[-2:]))
    # go pixel-by-pixel
    for i in range(cube.shape[-2]):
        for j in range(cube.shape[-1]):
            stokes_cube[:, i, j] = np.linalg.lstsq(mueller_matrices, cube[:, i, j], rcond=None)[0]

    return stokes_cube[:3]


def polarization_calibration_leastsq(filenames, mm_filenames, outname, force=False):
    path = Path(outname)
    if not force and path.is_file() and not any_file_newer(filenames, path):
        return path
    # step 1: load each file and header- derotate on the way
    cubes = []
    headers = []
    mueller_mats = []
    for file, mm_file in tqdm.tqdm(
        zip(filenames, mm_filenames, strict=True),
        total=len(filenames),
        desc="Least-squares calibration",
    ):
        cube, hdr = load_fits(file, header=True, memmap=False)
        # rotate to N up E left
        cube_derot = derotate_frame(cube, hdr["DEROTANG"])
        # get row vector for mueller matrix of each file
        mueller_mat = load_fits(mm_file, memmap=False)[0]

        cubes.append(cube_derot)
        headers.append(hdr)
        mueller_mats.append(mueller_mat)

    mueller_mat_cube = np.array(mueller_mats)
    super_cube = np.array(cubes)
    stokes_hdr = combine_frames_headers(headers, wcs=True)
    stokes_final = []
    for cube in super_cube:
        stokes_cube = mueller_matrix_calibration(mueller_mat_cube, cube)
        stokes_final.append(stokes_cube)
    stokes_final = np.array(stokes_final)
    return write_stokes_products(stokes_final, stokes_hdr, outname=path, force=True)


DOUBLEDIFF_SETS = set(itertools.product((0, 45, 22.5, 67.5), (1, 2)))
TRIPLEDIFF_SETS = set(itertools.product((0, 45, 22.5, 67.5), ("A", "B"), (1, 2)))


def get_triplediff_set(table, row):
    time_arr = Time(table["MJD"], format="mjd")
    row_time = Time(row["MJD"], format="mjd")
    deltatime = np.abs(row_time - time_arr)
    row_key = (row["RET-ANG1"], row["U_FLC"], row["U_CAMERA"])
    remaining_keys = TRIPLEDIFF_SETS - set(row_key)
    output_set = {row_key: row["path"]}
    for key in remaining_keys:
        mask = (
            (table["RET-ANG1"] == key[0])
            & (table["U_FLC"] == key[1])
            & (table["U_CAMERA"] == key[2])
        )
        idx = deltatime[mask].argmin()

        output_set[key] = table.loc[mask, "path"].iloc[idx]

    return output_set


def get_doublediff_set(table, row):
    time_arr = Time(table["MJD"], format="mjd")
    row_time = Time(row["MJD"], format="mjd")
    deltatime = np.abs(row_time - time_arr)
    row_key = (row["RET-ANG1"], row["U_CAMERA"])
    remaining_keys = DOUBLEDIFF_SETS - set(row_key)
    output_set = {row_key: row["path"]}
    for key in remaining_keys:
        mask = (table["RET-ANG1"] == key[0]) & (table["U_CAMERA"] == key[1])
        idx = deltatime[mask].argmin()

        output_set[key] = table.loc[mask, "path"].iloc[idx]

    return output_set


def make_stokes_image(
    path_set,
    outpath: Path,
    mm_paths=None,
    method="triplediff",
    mm_correct=True,
    ip_correct=True,
    ip_radius=8,
    ip_radius2=8,
    ip_method="photometry",
    force=False,
):
    if not force and outpath.exists() and not any_file_newer(path_set, outpath):
        return outpath

    # create stokes cube
    if method == "triplediff":
        stokes_hdul = polarization_calibration_triplediff(path_set)
        if mm_correct:
            mm_dict = make_triplediff_dict(mm_paths)
            _, mmQs, mmUs = triple_diff_dict(mm_dict)
    elif method == "doublediff":
        stokes_hdul = polarization_calibration_doublediff(path_set)
        if mm_correct:
            mm_dict = make_doublediff_dict(mm_paths)
            _, mmQs, mmUs = double_diff_dict(mm_dict)
    else:
        msg = f"Unrecognized method {method}"
        raise ValueError(msg)

    output_data = []
    output_data_err = []
    output_hdrs = []
    # slightly fragile, assumes HDUList is stored as
    # prim_hdu, *field, *field_err
    nfields = stokes_hdul[0].shape[0]
    for i in range(1, nfields + 1):
        hdu = stokes_hdul[i]
        stokes_data = hdu.data
        stokes_header = hdu.header
        error_extname = f"{stokes_header['FIELD']}ERR"
        stokes_err = stokes_hdul[error_extname].data
        I, Q, U = stokes_data  # noqa: E741
        I_err = stokes_err
        # mm correct
        if mm_correct:
            # get first row of diffed Mueller-matrix
            mmQ = mmQs[i - 1, 0]
            mmU = mmUs[i - 1, 0]

            # correct IP
            Q -= mmQ[0] * I
            U -= mmU[0] * I
            Q_err = np.hypot(I_err, mmQ[0] * I_err)
            U_err = np.hypot(I_err, mmU[0] * I_err)

            # correct cross-talk
            Sarr = np.array((Q.ravel(), U.ravel()))
            Marr = np.array((mmQ[1:3], mmU[1:3]))
            res = np.linalg.lstsq(Marr.T, Sarr, rcond=None)[0]
            Q = res[0].reshape(I.shape[-2:])
            U = res[1].reshape(I.shape[-2:])
            stokes_data = np.array((I, Q, U))
            stokes_err_data = np.array((I_err, Q_err, U_err))
        # second order IP correction
        if ip_correct:
            stokes_data, stokes_header = polarization_ip_correct(
                stokes_data,
                phot_rad=(ip_radius, ip_radius2),
                method=ip_method,
                header=stokes_header,
            )
            stokes_err_data[1] = np.hypot(
                stokes_err_data[1], stokes_header["IP_FQ"] * stokes_err_data[0]
            )
            stokes_err_data[2] = np.hypot(
                stokes_err_data[2], stokes_header["IP_FU"] * stokes_err_data[0]
            )
        stokes_header["STOKES"] = "I,Q,U", "Stokes axis data type"
        output_data.append(stokes_data)
        output_data_err.append(stokes_err_data)
        output_hdrs.append(stokes_header)

    prim_hdr = apply_wcs(combine_frames_headers(output_hdrs), angle=0)
    prim_hdu = fits.PrimaryHDU(np.array(output_data), prim_hdr)
    hdul = fits.HDUList(prim_hdu)
    for frame, hdr in zip(output_data, output_hdrs, strict=True):
        hdu = fits.ImageHDU(frame, header=hdr, name=hdr["FIELD"])
        hdul.append(hdu)
    for frame_err, hdr in zip(output_data_err, output_hdrs, strict=True):
        hdu = fits.ImageHDU(frame_err, header=hdr, name=f"{hdr['FIELD']}ERR")
        hdul.append(hdu)
    hdul.writeto(outpath, overwrite=True)
    return outpath


def polarization_ip_correct(stokes_data, phot_rad, method, header=None):
    match method:
        case "aperture":
            cQ = measure_instpol(stokes_data[0], stokes_data[1], r=phot_rad[0])
            cU = measure_instpol(stokes_data[0], stokes_data[2], r=phot_rad[0])
        case "annulus":
            cQ = measure_instpol_ann(
                stokes_data[0], stokes_data[1], Rin=phot_rad[0], Rout=phot_rad[1]
            )
            cU = measure_instpol_ann(
                stokes_data[0], stokes_data[2], Rin=phot_rad[0], Rout=phot_rad[1]
            )

    stokes_data[:3] = instpol_correct(stokes_data[:3], cQ, cU)

    if header is not None:
        header["IP_FQ"] = cQ, "I -> Q IP correction value"
        header["IP_FU"] = cU, "I -> U IP correction value"
    return stokes_data, header