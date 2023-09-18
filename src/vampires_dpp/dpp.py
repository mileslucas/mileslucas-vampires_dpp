import glob
import multiprocessing as mp
import os
import readline
import shutil
from multiprocessing import cpu_count
from pathlib import Path

import astropy.units as u
import click
import numpy as np
import tomli
import tqdm.auto as tqdm
from astropy.io import fits

import vampires_dpp as dpp
from vampires_dpp.calibration import (
    make_master_background,
    make_master_flat,
    normalize_file,
)
from vampires_dpp.constants import DEFAULT_NPROC
from vampires_dpp.organization import check_files, header_table, sort_files
from vampires_dpp.pipeline.config import (
    AnalysisConfig,
    CamFileInput,
    CollapseConfig,
    FrameSelectConfig,
    ObjectConfig,
    PipelineConfig,
    RegisterConfig,
)
from vampires_dpp.pipeline.deprecation import upgrade_config
from vampires_dpp.pipeline.pipeline import Pipeline
from vampires_dpp.pipeline.templates import (
    DEFAULT_DIRS,
    VAMPIRES_BLANK,
    VAMPIRES_PDI,
    VAMPIRES_SDI,
    VAMPIRES_SINGLECAM,
)
from vampires_dpp.util import check_version
from vampires_dpp.wcs import get_gaia_astrometry

from .cli.frame_centers import centroid


# callback that will confirm if a flag is false
def abort_if_false(ctx, param, value):
    if not value:
        ctx.abort()


def abort_if_true(ctx, param, value):
    if value:
        ctx.abort()


########## main ##########


@click.group(name="main", no_args_is_help=True)
@click.version_option(dpp.__version__, "--version", "-v", prog_name="vampires_dpp")
def main():
    pass


########## sort ##########


@main.command(
    name="sort",
    short_help="Sort raw data",
    help="Sorts raw data based on the data type. This will either use the `DATA-TYP` header value or the `U_OGFNAM` header, depending on when your data was taken.",
)
@click.argument(
    "filenames",
    nargs=-1,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--outdir",
    "-o",
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    default=Path.cwd(),
    help="Output directory.",
)
@click.option(
    "--num-proc",
    "-j",
    default=DEFAULT_NPROC,
    type=click.IntRange(1, cpu_count()),
    help="Number of processes to use.",
    show_default=True,
)
@click.option("--ext", "-e", default=0, help="HDU extension")
@click.option(
    "--copy/--no-copy",
    "-c/",
    callback=abort_if_false,
    prompt="Are you sure you want to move data files?",
    help="copy files instead of moving them",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Silence progress bars and extraneous logging.",
)
def sort_raw(filenames, outdir=Path.cwd(), num_proc=DEFAULT_NPROC, ext=0, copy=False, quiet=False):
    sort_files(
        filenames,
        copy=copy,
        ext=ext,
        output_directory=outdir,
        num_proc=num_proc,
        quiet=quiet,
    )


########## prep ##########


@click.group(
    name="prep",
    short_help="Prepare calibration files",
    help="Create calibration files from background files (darks or sky frames) and flats.",
)
@click.option(
    "--outdir",
    "-o",
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    default=Path.cwd(),
    help="Output directory.",
)
@click.option(
    "--num-proc",
    "-j",
    default=DEFAULT_NPROC,
    type=click.IntRange(1, cpu_count()),
    help="Number of processes to use.",
    show_default=True,
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Silence progress bars and extraneous logging.",
)
@click.pass_context
def prep(ctx, outdir, quiet, num_proc):
    # prepare context
    ctx.ensure_object(dict)
    ctx.obj["outdir"] = outdir
    ctx.obj["quiet"] = quiet
    ctx.obj["num_proc"] = num_proc


@prep.command(
    name="back",
    short_help="background files (darks/skies)",
    help="Create background files from darks/skies. Each input file will be collapsed. Groups of files with the same exposure time, EM gain, and frame size will be median-combined together to create a super-background file.",
)
@click.argument(
    "filenames",
    nargs=-1,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--collapse",
    "-c",
    type=click.Choice(["median", "mean", "varmean", "biweight"], case_sensitive=False),
    default="median",
    help="Frame collapse method",
    show_default=True,
)
@click.option("--force", "-f", is_flag=True, help="Force processing of files")
@click.pass_context
def back(ctx, filenames, collapse, force):
    make_master_background(
        filenames,
        collapse=collapse,
        force=force,
        output_directory=ctx.obj["outdir"],
        quiet=ctx.obj["quiet"],
        num_proc=ctx.obj["num_proc"],
    )


@prep.command(
    name="flat",
    short_help="flat-field files",
    help="Create flat-field files. Each input file will be collapsed with background-subtraction if files are provided. Groups of files with the same exposure time, EM gain, frame size, and filter will be median-combined together to create a super-flat file.",
)
@click.argument(
    "filenames",
    nargs=-1,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--back",
    "-b",
    type=click.Path(exists=True, path_type=Path),
    help="Background file to subtract from each flat-field. If a directory, will match the background files in that directory to the exposure times, EM gains, and frame sizes. Note: will search the output directory for background files if none are given.",
)
@click.option(
    "--collapse",
    "-c",
    type=click.Choice(["median", "mean", "varmean", "biweight"], case_sensitive=False),
    default="median",
    help="Frame collapse method",
    show_default=True,
)
@click.option("--force", "-f", is_flag=True, help="Force processing of files")
@click.pass_context
def flat(ctx, filenames, back, collapse, force):
    # if directory, filter non-FITS files and sort for background files
    background_files = []
    if back is None:
        back = ctx.obj["outdir"]
    if back.is_dir():
        fits_files = (
            list(back.rglob("*.fits.*")) + list(back.rglob("*.fts.*")) + list(back.rglob("*.fit.*"))
        )
        background_files.extend(
            filter(lambda f: fits.getkey(f, "CAL_TYPE") == "BACKGROUND", fits_files)
        )
    make_master_flat(
        filenames,
        collapse=collapse,
        force=force,
        output_directory=ctx.obj["outdir"],
        quiet=ctx.obj["quiet"],
        num_proc=ctx.obj["num_proc"],
    )


########## new ##########


def pathCompleter(text, state):
    """
    This is the tab completer for systems paths.
    Only tested on *nix systems
    """
    # replace ~ with the user's home dir. See https://docs.python.org/2/library/os.path.html
    if "~" in text:
        text = os.path.expanduser(text)

    # autocomplete directories with having a trailing slash
    if os.path.isdir(text):
        text += "/"

    return [x for x in glob.glob(text + "*")][state]


def createListCompleter(items):
    """
    This is a closure that creates a method that autocompletes from
    the given list.

    Since the autocomplete function can't be given a list to complete from
    a closure is used to create the listCompleter function with a list to complete
    from.
    """
    list_strings = map(str, items)

    def listCompleter(text, state):
        if not text:
            return list(list_strings)[state]
        else:
            matches = filter(lambda s: s.startswith(text), list_strings)
            return list(matches)[state]

    return listCompleter


@main.command(name="new", help="Generate configuration files")
@click.argument(
    "config",
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--edit", "-e", is_flag=True, help="Launch configuration file in editor after creation."
)
@click.pass_context
def new_config(ctx, config, edit):
    readline.set_completer_delims(" \t\n;")
    readline.parse_and_bind("tab: complete")

    ## check if output file exists
    if config.is_file():
        overwrite = click.confirm(
            f"{config.name} already exists in output directory, would you like to overwrite it?",
            default=False,
        )
        if not overwrite:
            ctx.exit()

    ## get template
    template_choices = ["none", "singlecam", "pdi", "sdi"]

    readline.set_completer(createListCompleter(template_choices))
    template = click.prompt(
        "Choose a starting template",
        type=click.Choice(template_choices, case_sensitive=False),
        default="none",
    )
    match template:
        case "singlecam":
            tpl = VAMPIRES_SINGLECAM
        case "pdi":
            tpl = VAMPIRES_PDI
        case "sdi":
            tpl = VAMPIRES_SDI
        case _:
            tpl = VAMPIRES_BLANK
    readline.set_completer()

    ## get name
    name_guess = config.stem
    name = click.prompt(f"Path-friendly name for this reduction", default=name_guess)
    tpl.name = name_guess if name == "" else name.replace(" ", "_").replace("/", "")

    ## get target
    object = click.prompt("SIMBAD-friendly object name (optional)", default="")
    obj = None if object == "" else object
    coord = None
    if obj is not None:
        rad = 1
        cat = "dr3"
        while True:
            coord = get_gaia_astrometry(obj, catalog=cat, radius=rad)
            if coord is not None:
                break

            click.echo(f'  Could not find {obj} in GAIA {cat.upper()} with {rad}" radius.')
            _input = click.prompt(
                "Query different catalog (dr1/dr2/dr3), enter search radius in arcsec, or enter new object name (optional)"
            )
            match _input:
                case "":
                    # give up
                    break
                case "dr1" | "dr2" | "dr3":
                    # try different catalog
                    cat = _input
                case _:
                    try:
                        # if a number was entered, increase search radius
                        rad = float(_input)
                    except ValueError:
                        # otherwise try a new object
                        obj = _input

        if coord is not None:
            tpl.coordinate = ObjectConfig(
                object=obj,
                ra=coord.ra.to_string("hour", sep=":", pad=True),
                dec=coord.dec.to_string("deg", sep=":", pad=True),
                parallax=coord.distance.to(u.mas, equivalencies=u.parallax()).value,
                pm_ra=coord.pm_ra_cosdec.to(u.mas / u.year).value,
                pm_dec=coord.pm_dec.to(u.mas / u.year).value,
                frame=coord.frame.name,
                obstime=str(coord.obstime),
            )
    if coord is None:
        click.echo("  No coordinate information set; will only use header values.")

    ## backgrounds
    have_backgrounds = click.confirm("Do you have background files?", default=True)
    if have_backgrounds:
        readline.set_completer(pathCompleter)
        cam1_path = click.prompt(
            "Enter path to cam1 background (optional)",
            default="",
            type=click.Path(dir_okay=False, path_type=Path),
        )
        cam1_path = None if cam1_path == "" else cam1_path
        cam2_path = None
        if template != "singlecam":
            if cam1_path is not None:
                cam2_default = str(cam1_path).replace("cam1", "cam2")
                cam2_path = click.prompt(
                    f"Enter path to cam2 background",
                    default=cam2_default,
                    type=click.Path(dir_okay=False, path_type=Path),
                )
            else:
                cam2_path = click.prompt(
                    "Enter path to cam2 background (optional)",
                    default="",
                    type=click.Path(dir_okay=False, path_type=Path),
                )
                if cam2_path == "":
                    cam2_path = None
        readline.set_completer()
        tpl.calibrate.master_backgrounds = CamFileInput(cam1=cam1_path, cam2=cam2_path)

    ## flats
    have_flats = click.confirm("Do you have flat files?", default=have_backgrounds)
    if have_flats:
        readline.set_completer(pathCompleter)
        cam1_path = click.prompt(
            "Enter path to cam1 flat (optional)",
            default="",
            type=click.Path(dir_okay=False, path_type=Path),
        )
        cam1_path = None if cam1_path == "" else cam1_path
        cam2_path = None
        if template != "singlecam" or cam1_path is None:
            if cam1_path is not None:
                cam2_default = cam1_path.replace("cam1", "cam2")
                cam2_path = click.prompt(
                    "Enter path to cam2 flat",
                    default=cam2_default,
                    type=click.Path(dir_okay=False, path_type=Path),
                )
                if cam2_path == "":
                    cam2_path = cam2_default
            else:
                cam2_path = click.prompt(
                    "Enter path to cam2 flat (optional)",
                    default="",
                    type=click.Path(dir_okay=False, path_type=Path),
                )
                if cam2_path == "":
                    cam2_path = None
        readline.set_completer()
        tpl.calibrate.master_flats = CamFileInput(cam1=cam1_path, cam2=cam2_path)

    ## Coronagraph
    iwa_choices = ["36", "55", "92", "129"]

    readline.set_completer(createListCompleter(iwa_choices))
    tpl.coronagraphic = click.confirm("Did you use a coronagraph?", default=False)

    ## Frame selection
    do_frame_select = click.confirm(
        "Would you like to do frame selection?", default=tpl.frame_select is not None
    )
    if do_frame_select:
        cutoff = click.prompt(
            "  Enter a cutoff quantile (0 to 1, larger means more discarding)", type=float
        )

        metric_choices = ["normvar", "l2norm", "peak"]
        readline.set_completer(createListCompleter(metric_choices))
        frame_select_metric = click.prompt(
            "  Choose a frame selection metric",
            type=click.Choice(metric_choices, case_sensitive=False),
            default="normvar",
        )
        readline.set_completer()
        tpl.frame_select = FrameSelectConfig(
            cutoff=cutoff,
            metric=frame_select_metric,
        )
    else:
        tpl.frame_select = None

    ## Registration
    do_register = click.confirm(
        f"Would you like to do frame registration?", default=tpl.register is not None
    )
    if do_register:
        method_choices = ["com", "peak", "dft", "moffat", "gaussian", "airy"]
        readline.set_completer(createListCompleter(method_choices))
        register_method = click.prompt(
            "  Choose a registration method",
            type=click.Choice(method_choices, case_sensitive=False),
            default="com",
        )
        readline.set_completer()
        opts = RegisterConfig(method=register_method)
        if register_method == "dft":
            opts.dft_factor = click.prompt("    Enter DFT upsample factor", default=1, type=int)

        opts.smooth = click.confirm("  Smooth data before measurement?", default=False)
        tpl.register = opts
    else:
        tpl.register = None

    ## Collapsing
    do_collapse = click.confirm(
        "Would you like to collapse your data?", default=tpl.collapse is not None
    )
    if do_collapse:
        collapse_choices = ["median", "mean", "varmean", "biweight"]
        readline.set_completer(createListCompleter(collapse_choices))
        collapse_method = click.prompt(
            "  Choose a collapse method",
            type=click.Choice(collapse_choices, case_sensitive=False),
            default="median",
        )
        readline.set_completer()
        tpl.collapse = CollapseConfig(collapse_method)
    else:
        tpl.collapse = None

    tpl.make_diff_images = click.confirm(
        "Would you like to make difference images?", default=tpl.diff is not None
    )

    if click.confirm("Would you like to do PSF analysis?", default=tpl.analysis is not None):
        model = click.prompt(
            "  Choose a PSF model",
            type=click.Choice(["gaussian", "moffat", "airydisk"]),
            default="gaussian",
        )
        subtract_radprof = click.confirm(
            "  Would you like to subtract a radial profile?", default=tpl.coronagraph is not None
        )

        aper_rad = click.prompt("  Enter aperture radius (px)", type=float, default=10)
        window_size = click.prompt("  Enter window size (px)", type=float, default=40)
        if aper_rad > window_size / 2:
            aper_rad = window_size / 2
            click.echo(f"Reducing aperture radius to match window size ({aper_rad:.0f} px)")
        ann_rad = None
        if click.confirm("  Would you like to subtract background annulus?", default=False):
            resp = click.prompt(
                "  Enter comma-separated inner and outer radius (px)",
                default=f"{aper_rad+5}, {aper_rad+10}",
            )
            ann_rad = list(_parse_cam_center(resp))
            if ann_rad[1] > window_size / 2:
                ann_rad[1] = window_size / 2
                click.echo(
                    f"Reducing annulus outer radius to match window size ({ann_rad[1]:.0f} px)"
                )

            if ann_rad[0] > ann_rad[1]:
                ann_rad[0] = ann_rad[1] - 1
                click.echo(
                    f"Reducing annulus inner radius to less than outer radius ({ann_rad[0]:.0f} px)"
                )

            if aper_rad > ann_rad[0]:
                aper_rad = ann_rad[0]
                click.echo(
                    f"Reducing aperture radius to less than annulus radius ({aper_rad:.0f} px)"
                )

        tpl.analysis = AnalysisConfig(
            model=model,
            window_size=window_size,
            subtract_radprof=subtract_radprof,
            aper_rad=aper_rad,
            ann_rad=ann_rad,
        )
    else:
        tpl.analysis = None

    ## Polarization
    if click.confirm("Would you like to do polarimetry?", default=tpl.polarimetry is not None):
        calib_choices = ["difference", "leastsq"]
        readline.set_completer(createListCompleter(calib_choices))
        tpl.polarimetry.method = click.prompt(
            "  Choose a polarimetric calibration method",
            type=click.Choice(calib_choices, case_sensitive=False),
            default="difference",
        )
        readline.set_completer()
        if tpl.polarimetry.method == "difference":
            tpl.polarimetry.mm_correct = click.confirm(
                "  Would you like to to Mueller-matrix correction?",
                default=True,
            )
        tpl.polarimetry.ip_correct = click.confirm(
            "  Would you like to do IP touchup?", default=tpl.polarimetry.ip is not None
        )

    else:
        tpl.polarimetry = None

    tpl.to_file(config)
    click.echo(f"File saved to {config.name}")
    if not edit:
        edit |= click.confirm("Would you like to edit this config file now?")

    if edit:
        click.edit(filename=config)


def _parse_cam_center(input_string):
    toks = input_string.replace(" ", "").split(",")
    ctr = list(map(float, toks))
    return ctr


########## normalize ##########


@main.command(name="norm", help="Normalize VAMPIRES data files")
@click.argument(
    "filenames",
    nargs=-1,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option("-o", "--outdir", type=Path, default=Path.cwd() / "prep", help="Output directory")
@click.option(
    "-d/-nd",
    "--deint/--no-deint",
    default=False,
    help="Deinterleave files into FLC states (WARNING: only apply this to old VAMPIRES data downloaded directly from `sonne`)",
)
@click.option(
    "-f/-nf",
    "--filter-empty/--no-filter-empty",
    default=True,
    help="Filter empty frames from data (post deinterleaving, if applicable)",
)
@click.option(
    "--num-proc",
    "-j",
    default=DEFAULT_NPROC,
    type=click.IntRange(1, cpu_count()),
    help="Number of processes to use.",
    show_default=True,
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Silence progress bars and extraneous logging.",
)
def norm(
    filenames, deint: bool, filter_empty: bool, num_proc: int, quiet: bool, output_directory: Path
):
    jobs = []
    kwargs = dict(deinterleave=deint, filter_empty=filter_empty, output_directory=output_directory)
    with mp.Pool(num_proc) as pool:
        for filename in filenames:
            jobs.append(pool.apply_async(normalize_file, args=(filename,), kwds=kwargs))

        iter = jobs if quiet else tqdm.tqdm(jobs, desc="Normalizing files")
        results = [job.get() for job in iter]

    return results


########## process ##########


@main.command(name="process", help="Run the data processing pipeline")
@click.argument(
    "config",
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.argument(
    "filenames",
    nargs=-1,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option("-o", "--outdir", default=Path.cwd(), type=Path, help="Output file directory")
@click.option(
    "--num-proc",
    "-j",
    default=DEFAULT_NPROC,
    type=click.IntRange(1, cpu_count()),
    help="Number of processes to use.",
    show_default=True,
)
def process(config: Path, filenames, num_proc, outdir):
    # make sure versions match within SemVar
    pipeline = Pipeline(PipelineConfig.from_file(config), workdir=outdir)
    if not check_version(pipeline.config.dpp_version, dpp.__version__):
        raise ValueError(
            f"Input pipeline version ({pipeline.config.dpp_version}) is not compatible with installed version of `vampires_dpp` ({dpp.__version__}). Try running `dpp upgrade {config}`."
        )
    shutil.copyfile(config, pipeline.product_dir / config.name.replace(".toml", ".bak.toml"))
    pipeline.run(filenames, num_proc=num_proc)


########## pdi ##########


@main.command(name="pdi", help="Run the polarimetric differential imaging pipeline")
@click.argument(
    "config",
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.argument(
    "filenames",
    nargs=-1,
    default=None,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--num-proc",
    "-j",
    default=DEFAULT_NPROC,
    type=click.IntRange(1, cpu_count()),
    help="Number of processes to use.",
    show_default=True,
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Silence progress bars and extraneous logging.",
)
def pdi(config, filenames, num_proc, quiet):
    # make sure versions match within SemVar
    pipeline = Pipeline.from_file(config)
    # get filenames from expected outputs
    if filenames is None:
        pass
    pipeline._polarimetry()


########## table ##########


@main.command(
    name="table",
    short_help="Create CSV from headers",
    help="Go through each file and combine the header information into a single CSV.",
)
@click.argument(
    "filenames",
    nargs=-1,
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=(Path.cwd() / "header_table.csv").name,
    help="Output path.",
    show_default=True,
)
@click.option(
    "--num-proc",
    "-j",
    default=DEFAULT_NPROC,
    type=click.IntRange(1, cpu_count()),
    help="Number of processes to use.",
    show_default=True,
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Silence progress bars and extraneous logging.",
)
def table(filenames, output, num_proc, quiet):
    # handle name clashes
    outpath = Path(output).resolve()
    if outpath.is_file():
        click.confirm(
            f"{outpath.name} already exists in the output directory. Overwrite?", abort=True
        )
    df = header_table(filenames, num_proc=num_proc, quiet=quiet)
    df.to_csv(outpath)


########## upgrade ##########


@main.command(
    name="upgrade",
    short_help="Upgrade configuration file",
    help=f"Tries to automatically upgrade a configuration file to the current version ({dpp.__version__}), prompting where necessary.",
)
@click.argument(
    "config",
    type=click.Path(dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Output path.",
)
@click.option(
    "--edit", "-e", is_flag=True, help="Launch configuration file in editor after creation."
)
def upgrade(config, output, edit):
    if output is None:
        click.confirm("Are you sure you want to modify your configuration in-place?", abort=True)
        output = config
    with open(config, "rb") as fh:
        input_toml = tomli.load(fh)
    output_config = upgrade_config(input_toml)
    output_config.to_file(output)
    click.echo(f"File saved to {output.name}")
    if not edit:
        edit |= click.confirm("Would you like to edit this config file now?")

    if edit:
        click.edit(filename=output)


if __name__ == "__main__":
    main()
