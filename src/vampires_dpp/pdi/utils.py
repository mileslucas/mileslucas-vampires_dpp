from pathlib import Path

import numpy as np
from astropy.io import fits
from numpy.typing import ArrayLike, NDArray

from vampires_dpp.analysis import safe_annulus_sum, safe_aperture_sum
from vampires_dpp.image_processing import combine_frames_headers
from vampires_dpp.indexing import frame_angles
from vampires_dpp.wcs import apply_wcs


def measure_instpol(I: NDArray, X: NDArray, r=5, expected=0):  # noqa: E741
    """Use aperture photometry to estimate the instrument polarization.

    Parameters
    ----------
    stokes_cube : NDArray
        Input Stokes cube (4, y, x)
    r : float, optional
        Radius of circular aperture in pixels, by default 5
    expected : float, optional
        The expected fractional polarization, by default 0
    **kwargs

    Returns
    -------
    float
        The instrumental polarization coefficient
    """
    x = X / I
    pX, _ = safe_aperture_sum(x, r=r)
    return pX / (np.pi * r**2) - expected


def measure_instpol_ann(I: NDArray, X: NDArray, Rin, Rout, expected=0):  # noqa: E741
    x = X / I
    pX, _ = safe_annulus_sum(x, Rin, Rout)
    return pX / (np.pi * (Rout**2 - Rin**2)) - expected


def instpol_correct(stokes_cube: NDArray, pQ=0, pU=0):
    """Apply instrument polarization correction to stokes cube.

    Parameters
    ----------
    stokes_cube : NDArray
        (3, ...) array of stokes values
    pQ : float, optional
        I -> Q contribution, by default 0
    pU : float, optional
        I -> U contribution, by default 0

    Returns
    -------
    NDArray
        (3, ...) stokes cube with corrected parameters
    """
    return np.array(
        (stokes_cube[0], stokes_cube[1] - pQ * stokes_cube[0], stokes_cube[2] - pU * stokes_cube[0])
    )


def radial_stokes(stokes_cube: ArrayLike, stokes_err: ArrayLike | None = None, phi: float = 0):
    r"""Calculate the radial Stokes parameters from the given Stokes cube (4, N, M)

    .. math::
        Q_\phi = -Q\cos(2\theta) - U\sin(2\theta) \\
        U_\phi = Q\sin(2\theta) - Q\cos(2\theta)


    Parameters
    ----------
    stokes_cube : ArrayLike
        Input Stokes cube, with dimensions (4, N, M)
    phi : float
        Radial angle offset in radians, by default 0

    Returns
    -------
    NDArray, NDArray
        Returns the tuple (Qphi, Uphi)
    """
    thetas = frame_angles(stokes_cube, conv="astro")

    cos2t = np.cos(2 * (thetas + phi))
    sin2t = np.sin(2 * (thetas + phi))
    Qphi = -cos2t * stokes_cube[1] - sin2t * stokes_cube[2]
    Uphi = sin2t * stokes_cube[1] - cos2t * stokes_cube[2]

    if stokes_err is not None:
        Qphi_err = np.hypot(cos2t * stokes_err[1], sin2t * stokes_err[2])
        Uphi_err = np.hypot(sin2t * stokes_err[1], cos2t * stokes_err[2])
    else:
        Qphi_err = Uphi_err = np.full_like(Qphi, np.nan)

    return Qphi, Uphi, Qphi_err, Uphi_err


def rotate_stokes(stokes_cube, theta):
    out = stokes_cube.copy()
    sin2ts = np.sin(2 * theta)
    cos2ts = np.cos(2 * theta)
    out[1] = stokes_cube[1] * cos2ts - stokes_cube[2] * sin2ts
    out[2] = stokes_cube[1] * sin2ts + stokes_cube[2] * cos2ts
    return out


def write_stokes_products(hdul, outname=None, force=False, phi=0):
    path = Path("stokes_cube.fits") if outname is None else Path(outname)

    if not force and path.is_file():
        return path

    output_data = []
    output_err = []
    output_hdrs = []
    stokes_data = hdul[0].data
    stokes_err = hdul["ERR"].data
    for i in range(stokes_data.shape[0]):
        data, err = stokes_products(stokes_data[i], stokes_err[i], phi=phi)

        hdr = hdul[2 + i].header
        hdr["STOKES"] = "I,Q,U,Qphi,Uphi,LP_I,AoLP", "Stokes axis data type"
        if phi is not None:
            hdr["AOLPPHI"] = phi, "[deg] offset angle for Qphi and Uphi"

        output_data.append(data)
        output_err.append(err)
        output_hdrs.append(hdr)

    prim_hdr = apply_wcs(stokes_data, combine_frames_headers(output_hdrs), angle=0)
    prim_hdu = fits.PrimaryHDU(np.squeeze(output_data), header=prim_hdr)
    err_hdu = fits.ImageHDU(np.squeeze(output_err), header=prim_hdr, name="ERR")
    hdul_out = fits.HDUList([prim_hdu, err_hdu])
    hdul_out.extend([fits.ImageHDU(header=hdr) for hdr in output_hdrs])
    hdul_out.writeto(path, overwrite=True)

    return path


def stokes_products(stokes_frame, stokes_err, phi=0):
    pi = np.hypot(stokes_frame[2], stokes_frame[1])
    aolp = np.arctan2(stokes_frame[2], stokes_frame[1])
    Qphi, Uphi, Qphi_err, Uphi_err = radial_stokes(stokes_frame, stokes_err, phi=phi)
    # error propagation
    pi_err = np.hypot(stokes_frame[2] * stokes_err[2], stokes_frame[1] * stokes_err[1]) / np.abs(pi)
    aolp_err = np.hypot(stokes_frame[1] * stokes_err[2], stokes_frame[2] * stokes_err[1]) / pi**2

    data = np.asarray((stokes_frame[0], stokes_frame[1], stokes_frame[2], Qphi, Uphi, pi, aolp))
    data_err = np.asarray(
        (stokes_err[0], stokes_err[1], stokes_err[2], Qphi_err, Uphi_err, pi_err, aolp_err)
    )
    return data, data_err
