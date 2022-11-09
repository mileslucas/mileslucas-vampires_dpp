#!/usr/bin/env python

from argparse import ArgumentParser
from astropy.io import fits
from pathlib import Path
import numpy as np
from vampires_dpp.lucky_imaging import lucky_image, measure_metric
import logging
import tqdm.auto as tqdm


parser = ArgumentParser()
parser.add_argument(
    "filename",
    nargs="+",
    help="FITS files to lucky image. If multiple files are provided, the frame selection metric will be calculated across all frames",
)
parser.add_argument("-q", "--quantile", type=float, default=0)
parser.add_argument(
    "-o", "--out", help="name of output file, by default will append name with `_lucky`"
)
parser.add_argument(
    "-m",
    "--metric",
    default="max",
    choices=["max", "min"],
    help="Frame selection metric",
)
parser.add_argument(
    "-r",
    "--register",
    default="max",
    choices=["max", "com", "dft"],
    help="Frame selection metric",
)
parser.add_argument(
    "--upsample-factor",
    default=10,
    help="if using DFT registration, use this upsampling factor",
)


def main():
    args = parser.parse_args()
    for filename in tqdm.tqdm(args.filename):
        _path = Path(filename)
        cube, hdr = fits.getdata(_path, header=True)
        logging.debug(f"file loaded from {cube}")
        sharp_frame = lucky_image(
            cube,
            args.quantile,
            metric=args.metric,
            register=args.register,
            upsample_factor=args.upsample_factor,
        )

        if args.out:
            outname = args.out
        else:
            outname = _path.with_name(f"{_path.stem}_lucky{_path.suffix}")
        logging.info(f"saving output to {outname}")
        fits.writeto(outname, sharp_frame, header=hdr, overwrite=True)


if __name__ == "__main__":
    main()