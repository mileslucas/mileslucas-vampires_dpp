import itertools
import logging
from typing import Literal, TypeAlias

import matplotlib.pyplot as plt
import numpy as np
from astropy.convolution import convolve_fft
from astropy.io import fits
from astropy.nddata import Cutout2D
from astropy.visualization import simple_norm
from image_registration import chi2_shift
from photutils import centroids
from skimage import filters

from vampires_dpp.headers import fix_header
from vampires_dpp.image_processing import shift_frame
from vampires_dpp.indexing import cutout_inds, frame_center, frame_radii, get_mbi_centers
from vampires_dpp.specphot.filters import determine_filterset_from_header
from vampires_dpp.synthpsf import create_synth_psf

__all__ = ("register_hdul",)

logger = logging.getLogger(__file__)

RegisterMethod: TypeAlias = Literal["peak", "com", "dft"]


def offset_dft(frame, inds, psf):
    cutout = frame[inds]
    xoff, yoff = chi2_shift(psf, cutout, upsample_factor="auto", return_error=False)
    dft_offset = np.array((yoff, xoff))
    ctr = np.array(frame_center(psf)) + dft_offset
    # offset based on    indices
    ctr[-2] += inds[-2].start
    ctr[-1] += inds[-1].start
    # plt.imshow(frame, origin="lower", cmap="magma")
    # # plt.imshow(psf, origin="lower", cmap="magma")
    # plt.scatter(ctr[-1], ctr[-2], marker='+', s=100, c="green")
    # plt.show(block=True)
    return ctr


def offset_peak_and_com(frame, inds):
    cutout = frame[inds]

    peak_yx = np.unravel_index(np.nanargmax(cutout), cutout.shape)
    com_xy = centroids.centroid_com(cutout)
    # offset based on indices
    offx = inds[-1].start
    offy = inds[-2].start
    ctrs = {
        "peak": np.array((peak_yx[0] + offy, peak_yx[1] + offx)),
        "com": np.array((com_xy[1] + offy, com_xy[0] + offx)),
    }
    return ctrs


def get_intersection(xs, ys):
    idxs = np.argsort(xs, axis=1)
    xs = np.take_along_axis(xs, idxs, axis=1)
    ys = np.take_along_axis(ys, idxs, axis=1)

    a = xs[:, 0] * ys[:, 3] - ys[:, 0] * xs[:, 3]
    b = xs[:, 1] * ys[:, 2] - ys[:, 1] * xs[:, 2]
    d = (xs[:, 0] - xs[:, 3]) * (ys[:, 1] - ys[:, 2]) - (ys[:, 0] - ys[:, 3]) * (
        xs[:, 1] - xs[:, 2]
    )
    px = (a * (xs[:, 1] - xs[:, 2]) - (xs[:, 0] - xs[:, 3]) * b) / d
    py = (a * (ys[:, 1] - ys[:, 2]) - (ys[:, 0] - ys[:, 3]) * b) / d

    return px, py


def get_centroids_from(metrics, input_key):
    cx = np.swapaxes(metrics[f"{input_key[:4]}x"], 0, 2)
    cy = np.swapaxes(metrics[f"{input_key[:4]}y"], 0, 2)
    # if there are values from multiple PSFs (e.g. satspots)
    # determine
    if cx.shape[1] == 4:
        cx, cy = get_intersection(cx, cy)
    else:
        cx, cy = cx[:, 0], cy[:, 0]

    # stack so size is (Nframes, Nfields, x/y)
    centroids = np.stack((cy, cx), axis=-1)
    return centroids


def register_hdul(
    hdul: fits.HDUList,
    metrics,
    *,
    align: bool = True,
    method: RegisterMethod = "dft",
    crop_width: int = 536,
) -> fits.HDUList:
    # load centroids
    # reminder, this has shape (nframes, nlambda, npsfs, 2)
    # take mean along PSF axis
    nframes, ny, nx = hdul[0].shape
    center = frame_center(hdul[0].data)
    header = hdul[0].header
    fields = determine_filterset_from_header(header)
    if align:
        centroids = get_centroids_from(metrics, method)
    elif "MBIR" in header["OBS-MOD"]:
        ctr_dict = get_mbi_centers(hdul[0].data, reduced=True)
        centroids = np.zeros((nframes, 3, 2))
        for idx, key in enumerate(fields):
            centroids[:, idx] = ctr_dict[key]
    elif "MBI" in header["OBS-MOD"]:
        ctr_dict = get_mbi_centers(hdul[0].data)
        centroids = np.zeros((nframes, 4, 2))
        for idx, key in enumerate(fields):
            centroids[:, idx] = ctr_dict[key]
    else:
        centroids = np.zeros((nframes, 1, 2))
        centroids[:] = center

    # determine maximum padding, with sqrt(2)
    # for radial coverage
    rad_factor = (crop_width / 2) * (np.sqrt(2) - 1)
    # round to nearest even number
    npad = int((rad_factor // 2) * 2)

    aligned_data = []
    aligned_err = []
    for tidx in range(centroids.shape[0]):
        frame = hdul[0].data[tidx]
        frame_err = hdul["ERR"].data[tidx]

        aligned_frames = []
        aligned_err_frames = []

        for wlidx in range(centroids.shape[1]):
            # determine offset for each field
            field_ctr = centroids[tidx, wlidx]
            # generate cutouts with crop width
            cutout = Cutout2D(frame, field_ctr[::-1], size=crop_width, mode="partial")
            cutout_err = Cutout2D(frame_err, field_ctr[::-1], size=crop_width, mode="partial")

            offset = field_ctr - cutout.position_original[::-1]

            # pad and shift data
            frame_padded = np.pad(cutout.data, npad, constant_values=np.nan)
            shifted = shift_frame(frame_padded, offset)
            aligned_frames.append(shifted)

            # pad and shift error
            frame_err_padded = np.pad(cutout_err.data, npad, constant_values=np.nan)
            shifted_err = shift_frame(frame_err_padded, offset)
            aligned_err_frames.append(shifted_err)
        aligned_data.append(aligned_frames)
        aligned_err.append(aligned_err_frames)

    aligned_cube = np.array(aligned_data)
    aligned_err_cube = np.array(aligned_err)
    # generate output HDUList
    output_hdul = fits.HDUList(
        [
            fits.PrimaryHDU(aligned_cube, header=hdul[0].header),
            fits.ImageHDU(aligned_err_cube, header=hdul["ERR"].header, name="ERR"),
        ]
    )
    for wlidx in range(centroids.shape[1]):
        hdr = header.copy()
        hdr["FIELD"] = fields[wlidx]
        output_hdul.append(fits.ImageHDU(header=hdr, name=hdr["FIELD"]))

    # update header info
    info = fits.Header()
    info["hierarch DPP ALIGN METHOD"] = method, "Frame alignment method"

    for hdu_idx in range(len(hdul)):
        output_hdul[hdu_idx].header.update(info)

    return output_hdul


def recenter_hdul(
    hdul: fits.HDUList,
    window_centers,
    *,
    method: RegisterMethod = "dft",
    window_size: int = 21,
    psfs: None = None,
):
    data_cube = hdul[0].data
    err_cube = hdul["ERR"].data
    # General strategy: use window centers to know where to search for PSFs
    # cast window_centers to array
    window_array = np.array(list(window_centers.values()))
    window_offsets = window_array - np.mean(window_array, axis=1, keepdims=True)
    field_center = frame_center(data_cube)
    ## Measure centroid
    for wl_idx in range(data_cube.shape[0]):
        frame = data_cube[wl_idx]
        offsets = []
        for offset in window_offsets[wl_idx]:
            inds = cutout_inds(frame, center=field_center + offset, window=window_size)
            match method:
                case "com" | "peak":
                    center = offset_peak_and_com(frame, inds)[method]
                case "dft":
                    assert psfs is not None
                    center = offset_dft(frame, inds, psf=psfs[wl_idx])

            offsets.append(field_center - center)
        offsets = np.array(offsets)
        if len(offsets) == 4:
            ox, oy = get_intersection(offsets[None, :, 1], offsets[None, :, 0])
            offset = np.array((oy[0], ox[0]))
        else:
            offset = offsets[0]
        data_cube[wl_idx] = shift_frame(frame, offset)
        err_cube[wl_idx] = shift_frame(err_cube[wl_idx], offset)

    info = fits.Header()
    info["hierarch DPP RECENTER"] = True, "Data was registered after coadding"
    info["hierarch DPP RECENTER METHOD"] = method, "DPP recentering registration method"

    for hdu in hdul:
        hdu.header.update(info)

    return hdul


def euclidean_distance(p1, p2):
    """Calculate distance between two points p1 and p2."""
    return np.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def find_right_triangles(vertices: list[tuple[int, int]], radius=3):
    """Find all sets of vertices that form a right triangle."""
    right_triangles = []

    # Check all combinations of three vertices
    for triplet in itertools.combinations(vertices, 3):
        p1, p2, p3 = triplet
        # Calculate squared distances
        dist_12 = euclidean_distance(p1, p2)
        dist_23 = euclidean_distance(p2, p3)
        dist_31 = euclidean_distance(p3, p1)

        # Sort distances to identify the longest one
        distances = np.sort((dist_12, dist_23, dist_31))

        if (
            np.isclose(distances[0], distances[1], atol=2 * radius)
            and np.isclose(distances[2] / np.mean(distances[:2]), np.sqrt(2), atol=5e-2)
            and np.all(distances > 38)
        ):
            right_triangles.append(triplet)

    return right_triangles


def test_triangle_plus_one_is_square(
    triangle: list[tuple[int, int]], point: tuple[int, int], radius: float = 3
):
    """Given a set of vertices that form a right triangles, see if the test point forms a square"""
    if point in triangle:
        return False
    vertices = [*triangle, point]

    distances = [
        euclidean_distance(pair[0], pair[1]) for pair in itertools.combinations(vertices, 2)
    ]
    distances.sort()

    diff1s = np.diff(distances[:4])
    diff2s = np.diff(distances[4:])
    return (
        np.isclose(np.mean(diff1s), 0, atol=2 * radius)
        and np.isclose(np.mean(diff2s), 0, atol=2 * radius)
        and np.isclose(np.mean(distances[4:]) / np.mean(distances[:4]), np.sqrt(2), atol=5e-2)
    )


def find_square_satspots(frame, radius=3, max_counter=50):
    # initialize
    frame = frame.copy().astype("f4")  # because we're writing NaN's in place
    max_ind = np.nanargmax(frame)

    counter = 0
    locs = []  # locs is a list of cartesian indices
    # tricombs is a ??
    tricombs = []

    # data structures for memoization
    while counter < max_counter:
        # grab newest spot candidate and add to list of locations
        inds = np.unravel_index(max_ind, frame.shape)
        locs.append(inds)
        frame = mask_circle(frame, inds, radius)
        # if we have at least four locations and some 3-spot candidates we can start to evaluate sets
        if len(locs) >= 4 and len(tricombs) >= 1:
            # generate all combinations of pairs of two coordinates, without repetition
            for last_spot in locs:
                for tricomb in reversed(tricombs):
                    if test_triangle_plus_one_is_square(tricomb, last_spot):
                        return [*tricomb, last_spot]

        # as soon as we have 3 spots, start the list of triangle candidates
        if len(locs) >= 3:
            # generate all combinations of pairs of two coordinates, without repetition
            triangle_sets = find_right_triangles(locs)
            tricombs.extend(triangle_sets)
        # setup next iteration
        max_ind = np.nanargmax(frame)
        counter += 1

    msg = "Could not fit satelliate spots!"
    print(msg)
    # raise RuntimeError(msg)
    return None


def mask_circle(frame, inds, radius):
    rs = frame_radii(frame, center=inds)
    mask = rs <= radius
    frame[mask] = np.nan
    return frame


def get_mbi_cutout(
    data, camera: int, field: Literal["F610", "F670", "F720", "F760"], reduced: bool = False
):
    hy, hx = frame_center(data)
    # use cam2 as reference
    match field:
        case "F610":
            x = hx * 0.25
            y = hy * 1.5
        case "F670":
            x = hx * 0.25
            y = hy * 0.5
        case "F720":
            x = hx * 0.75
            y = hy * 0.5
        case "F760":
            x = hx * 1.75
            y = hy * 0.5
        case _:
            msg = f"Invalid MBI field {field}"
            raise ValueError(msg)
    if reduced:
        y *= 2
    # flip y axis for cam 1 indices
    if camera == 1:
        y = data.shape[-2] - y
    return Cutout2D(data, (x, y), 300, mode="partial")


def autocentroid_hdul(
    hdul: fits.HDUList,
    coronagraphic: bool = False,
    psfs=None,
    crop_size=150,
    window_size=21,
    plot: bool = False,
):
    # collapse cubes, if need
    data = np.nanmedian(hdul[0].data, axis=0) if hdul[0].data.ndim == 3 else hdul[0].data
    # fix header
    header = fix_header(hdul[0].header)
    output = []
    fields = determine_filterset_from_header(header)
    if psfs is None:
        psfs = [create_synth_psf(header, filt, npix=window_size) for filt in fields]
    # for MBI data, divide input image into octants and account for
    # image flip between cam1 and cam2
    if "MBI" in header["OBS-MOD"]:
        reduced = "MBIR" in header["OBS-MOD"]
        cutouts = [
            get_mbi_cutout(data, header["U_CAMERA"], field, reduced=reduced) for field in fields
        ]
    # otherwise, just take the whole frame
    else:
        cutouts = [Cutout2D(data, frame_center(data)[::-1], data.shape[-1])]

    # for each frame (1, 3, or 4)
    for idx in range(len(fields)):
        psfs[idx] /= np.nansum(psfs[idx])
        # find centroid of image with square scaling to help bias towards PSF
        rough_ctr = centroids.centroid_com(cutouts[idx].data ** 2, mask=np.isnan(cutouts[idx].data))
        # take a large crop, large enough to see satellite spots plus misregistration
        rough_cutout = Cutout2D(cutouts[idx].data, rough_ctr, crop_size, mode="partial")
        # high-pass filter the data with a large median-- note, this requires the data to have
        # a certain level of S/N or it will wipe out the satellite spots. Therefore it's only suggested
        # to run the autocentroid on a big stack of mean-combined data instead of individual frames
        filtered_cutout = rough_cutout.data - filters.median(rough_cutout.data, np.ones((9, 9)))
        # convolve high-pass filtered data with the PSF for better S/N (unsharp-mask-ish)
        filtered_cutout = convolve_fft(filtered_cutout, psfs[idx])

        # when using the coronagraph, find four maxima which form a square
        if coronagraphic:
            points = find_square_satspots(filtered_cutout)
        else:
            # otherwise use DFT cross-correlation to find the PSF localized around peak index
            ctr = np.unravel_index(np.nanargmax(filtered_cutout), filtered_cutout.shape)
            inds = Cutout2D(
                filtered_cutout, ctr[::-1], size=psfs[idx].shape[-2:], mode="partial"
            ).slices_original
            points = [offset_dft(filtered_cutout, inds, psfs[idx])]
        # make sure to offset for indices
        rough_points = [rough_cutout.to_original_position(p[::-1]) for p in points]
        orig_points = [cutouts[idx].to_original_position(p)[::-1] for p in rough_points]
        output.append(orig_points)

        ## plotting
        if plot:
            fig, axs = plt.subplots(ncols=2)
            norm = simple_norm(cutouts[idx].data, stretch="sqrt")
            axs[0].imshow(cutouts[idx].data, origin="lower", cmap="magma", norm=norm)
            axs[0].scatter(*rough_ctr, marker="+", s=100, c="green")
            norm = None if coronagraphic else simple_norm(filtered_cutout, stretch="sqrt")
            axs[1].imshow(filtered_cutout, origin="lower", cmap="magma", norm=norm)

            axs[1].scatter(*rough_cutout.position_cutout, marker="+", s=100, c="green")

            if points is not None:
                xs = np.array([p[1] for p in points])
                ys = np.array([p[0] for p in points])
                axs[1].scatter(xs, ys, marker=".", s=100, c="cyan")
                if len(xs) == 4:
                    # plot lines
                    idxs = np.argsort(xs)
                    xs = xs[idxs]
                    ys = ys[idxs]
                    axs[1].plot([xs[0], xs[3]], [ys[0], ys[3]], c="cyan")
                    axs[1].plot([xs[1], xs[2]], [ys[1], ys[2]], c="cyan")
                    px, py = get_intersection(xs[None, :], ys[None, :])
                    axs[1].scatter(px, py, marker="+", s=100, c="cyan")
                else:
                    axs[1].scatter(xs[0], ys[0], marker="x", s=100, c="cyan")

            axs[0].set_title("Starting cutout")
            axs[1].set_title("Centroided cutout")
            fig.suptitle(fields[idx])
            fig.tight_layout()
            plt.show(block=True)

    return np.array(output)
