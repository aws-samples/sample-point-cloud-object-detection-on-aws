"""CLI entry point for the point cloud detection pipeline."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from pointcloud_detect.backends.checkpoints import NonCommercialWeightsDisabled
from pointcloud_detect.config import PipelineConfig

app = typer.Typer(
    name="pointcloud-detect",
    help="3D point cloud object detection and segmentation pipeline.",
    no_args_is_help=True,
)
console = Console()


def _load_config(config_path: Path | None) -> PipelineConfig:
    """Load config from YAML/JSON file or return defaults."""
    if config_path is None:
        return PipelineConfig()

    import orjson

    if config_path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as e:
            raise ImportError(
                "PyYAML is required for YAML config files. Install with: uv add pyyaml"
            ) from e
        data = yaml.safe_load(config_path.read_text())
    else:
        data = orjson.loads(config_path.read_bytes())

    return PipelineConfig.model_validate(data)


@app.command()
def detect(
    input_file: Annotated[
        Path,
        typer.Argument(
            help="Path to input point cloud file (E57, LAS, LAZ, or PLY)",
            exists=True,
            readable=True,
        ),
    ],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to pipeline config file (YAML or JSON)",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output directory (overrides config)",
        ),
    ] = None,
    model_mode: Annotated[
        str | None,
        typer.Option(
            "--mode",
            "-m",
            help="Model mode: indoor, outdoor, or custom",
        ),
    ] = None,
    confidence: Annotated[
        float | None,
        typer.Option(
            "--confidence",
            help="Confidence threshold (0.0-1.0)",
        ),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option(
            "--raw",
            help="Output all detections without confidence filtering",
        ),
    ] = False,
    normalize: Annotated[
        bool,
        typer.Option(
            "--normalize",
            help="Normalize output coordinates to [0,1] based on scene extents",
        ),
    ] = False,
    downsample: Annotated[
        float | None,
        typer.Option(
            "--downsample",
            help="Voxel size for downsampling in metres (0 to disable)",
        ),
    ] = None,
    use_color: Annotated[
        bool | None,
        typer.Option(
            "--use-color/--no-color",
            help="Include RGB channels for inference",
        ),
    ] = None,
    use_intensity: Annotated[
        bool | None,
        typer.Option(
            "--use-intensity/--no-intensity",
            help="Include intensity channel for inference",
        ),
    ] = None,
    use_normals: Annotated[
        bool | None,
        typer.Option(
            "--use-normals/--no-normals",
            help="Compute and include surface normals for inference",
        ),
    ] = None,
    coord_frame: Annotated[
        str | None,
        typer.Option(
            "--coord-frame",
            help="Source coordinate frame: z-up, y-up, x-up, or ros",
        ),
    ] = None,
    device: Annotated[
        str | None,
        typer.Option(
            "--device",
            help="Inference device: auto, cuda:0, cpu",
        ),
    ] = None,
    min_size: Annotated[
        float | None,
        typer.Option(
            "--min-size",
            help="Minimum detection size in metres (max bbox dimension)",
        ),
    ] = None,
    max_size: Annotated[
        float | None,
        typer.Option(
            "--max-size",
            help="Maximum detection size in metres (max bbox dimension)",
        ),
    ] = None,
    exclude_label: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-label",
            help="Label to exclude from output (repeatable)",
        ),
    ] = None,
    max_points: Annotated[
        int | None,
        typer.Option(
            "--max-points",
            help=(
                "Maximum points to load (for large files, e.g. 20000000). "
                "Auto-capped at 50M for E57 >500M points."
            ),
        ),
    ] = None,
    weights: Annotated[
        Path | None,
        typer.Option(
            "--weights",
            "-w",
            help=(
                "Detection weights you trained (a model.pth from the trainer). "
                "Without it the pretrained checkpoint is used, which needs "
                "ALLOW_NONCOMMERCIAL_WEIGHTS=1."
            ),
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    architecture: Annotated[
        str | None,
        typer.Option(
            "--architecture",
            help=(
                "Detection architecture the weights belong to: votenet_scannet, "
                "fcaf3d_scannet, centerpoint_nuscenes or pointpillars_kitti. "
                "Overrides the one --mode picks."
            ),
        ),
    ] = None,
) -> None:
    """Run 3D object detection and segmentation on a point cloud file."""
    pipeline_config = _load_config(config)

    if output_dir is not None:
        pipeline_config.output.directory = output_dir
    if model_mode is not None:
        pipeline_config.model.mode = model_mode  # type: ignore[assignment]
        if model_mode == "outdoor":
            pipeline_config.model.detection.config = "centerpoint_nuscenes"
            # CenterPoint voxelizer requires points within ±51.2m range
            pipeline_config.preprocessing.point_cloud_range = [
                -51.2,
                -51.2,
                -5.0,
                51.2,
                51.2,
                3.0,
            ]
        elif model_mode == "indoor":
            # Use FCAF3D if MinkowskiEngine is available, otherwise fall back to VoteNet
            try:
                import MinkowskiEngine  # noqa: F401  # pyright: ignore[reportMissingImports]

                pipeline_config.model.detection.config = "fcaf3d_scannet"
            except ImportError:
                pipeline_config.model.detection.config = "votenet_scannet"
            pipeline_config.postprocessing.nms_iou_threshold = 0.2
        elif model_mode == "kitti":
            pipeline_config.model.detection.config = "pointpillars_kitti"
            pipeline_config.model.segmentation.enabled = False
    if architecture is not None:
        pipeline_config.model.detection.config = architecture
    if weights is not None:
        pipeline_config.model.detection.weights = str(weights)
    if confidence is not None:
        pipeline_config.postprocessing.confidence_threshold = confidence
    if raw:
        pipeline_config.postprocessing.raw_mode = True
    if normalize:
        pipeline_config.output.normalize_coordinates = True
    if downsample is not None:
        if downsample == 0:
            pipeline_config.preprocessing.downsample = False
        else:
            pipeline_config.preprocessing.downsample = True
            pipeline_config.preprocessing.voxel_size_m = downsample
    if use_color is not None:
        pipeline_config.preprocessing.use_color = use_color
    if use_intensity is not None:
        pipeline_config.preprocessing.use_intensity = use_intensity
    if use_normals is not None:
        pipeline_config.preprocessing.use_normals = use_normals
    if coord_frame is not None:
        pipeline_config.preprocessing.coord_frame = coord_frame
    if device is not None:
        pipeline_config.inference.device = device
    if min_size is not None:
        pipeline_config.postprocessing.min_size_m = min_size
    if max_size is not None:
        pipeline_config.postprocessing.max_size_m = max_size
    if exclude_label is not None:
        pipeline_config.postprocessing.excluded_labels = exclude_label
    if max_points is not None:
        pipeline_config.preprocessing.max_points = max_points

    from pointcloud_detect.pipeline import run_pipeline

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        disable=not pipeline_config.logging.progress,
    ) as progress:
        progress.add_task("Running detection pipeline...", total=None)
        try:
            result = run_pipeline(input_file, pipeline_config)
        except MemoryError:
            console.print(
                "[bold red]Error:[/] Out of memory. "
                "Try --max-points to reduce point count."
            )
            raise SystemExit(1)
        except FileNotFoundError as e:
            console.print(f"[bold red]Error:[/] File not found: {e}")
            raise SystemExit(1)
        except ValueError as e:
            console.print(f"[bold red]Error:[/] Invalid input: {e}")
            raise SystemExit(1)
        except NonCommercialWeightsDisabled as e:
            console.print(f"[bold red]Error:[/] {e} Or pass --weights.")
            raise SystemExit(1)
        except Exception as e:
            console.print(f"[bold red]Error:[/] Pipeline failed: {e}")
            raise

    console.print(
        f"\n[bold green]Done.[/] "
        f"{len(result.detections)} detections across "
        f"{result.tile_count} tiles "
        f"in {result.processing_time_seconds:.1f}s"
    )
    console.print(f"Output: {pipeline_config.output.directory / '*.json'}")


@app.command()
def info(
    input_file: Annotated[
        Path,
        typer.Argument(
            help="Path to input point cloud file",
            exists=True,
            readable=True,
        ),
    ],
) -> None:
    """Display information about a point cloud file."""
    from pointcloud_detect.io_readers import detect_format, read_point_cloud

    fmt = detect_format(input_file)
    # Use a small sample for info to avoid OOM on large files
    cloud = read_point_cloud(input_file, max_points=1_000_000)

    console.print(f"[bold]File:[/] {input_file}")
    console.print(f"[bold]Format:[/] {fmt}")
    console.print(f"[bold]Points:[/] {cloud.num_points:,}")
    console.print(
        f"[bold]Bounds min:[/] "
        f"[{cloud.bounds_min[0]:.3f}, "
        f"{cloud.bounds_min[1]:.3f}, "
        f"{cloud.bounds_min[2]:.3f}]"
    )
    console.print(
        f"[bold]Bounds max:[/] "
        f"[{cloud.bounds_max[0]:.3f}, "
        f"{cloud.bounds_max[1]:.3f}, "
        f"{cloud.bounds_max[2]:.3f}]"
    )
    console.print(f"[bold]Has RGB:[/] {cloud.rgb is not None}")
    console.print(f"[bold]Has intensity:[/] {cloud.intensity is not None}")
    console.print(f"[bold]Has normals:[/] {cloud.normals is not None}")


@app.command()
def data(
    action: Annotated[
        str,
        typer.Argument(help="Action: download, parse, or both"),
    ] = "both",
    data_root: Annotated[
        Path | None,
        typer.Option(
            "--data-root",
            "-d",
            help="Root directory for dataset storage",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output directory for parsed annotations",
        ),
    ] = None,
    dataset: Annotated[
        str | None,
        typer.Option(
            "--dataset",
            help="Specific dataset: kitti, scannet, or s3dis (default: all)",
        ),
    ] = None,
) -> None:
    """Download and parse datasets for testing and fine-tuning."""
    from pointcloud_detect.data.download import (
        download_all,
        download_kitti,
        download_nuscenes_mini,
        download_s3dis_samples,
        download_scannet_samples,
    )
    from pointcloud_detect.data.parse import (
        parse_all,
        parse_kitti_dataset,
        parse_s3dis_dataset,
        parse_scannet_dataset,
    )

    root = data_root or Path("./data")

    if action in ("download", "both"):
        console.print("[bold]Downloading datasets...[/]")
        if dataset == "kitti":
            download_kitti(root)
        elif dataset == "scannet":
            download_scannet_samples(root)
        elif dataset == "s3dis":
            download_s3dis_samples(root)
        elif dataset == "nuscenes":
            download_nuscenes_mini(root)
        else:
            download_all(root)

    if action in ("parse", "both"):
        console.print("\n[bold]Parsing datasets into annotation format...[/]")
        out = output_dir or root / "annotations"

        if dataset == "kitti":
            parse_kitti_dataset(root / "kitti", out / "kitti")
        elif dataset == "scannet":
            parse_scannet_dataset(root / "scannet", out / "scannet")
        elif dataset == "s3dis":
            parse_s3dis_dataset(root / "s3dis", out / "s3dis")
        else:
            parse_all(root, out)

        console.print(f"\n[bold green]Annotations written to:[/] {out}")


@app.command()
def visualize(
    input_file: Annotated[
        Path,
        typer.Argument(
            help="Path to input point cloud file (.bin, .ply, etc.)",
            exists=True,
            readable=True,
        ),
    ],
    detections: Annotated[
        Path,
        typer.Argument(
            help="Path to detections JSON file",
            exists=True,
            readable=True,
        ),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output directory for HTML file",
        ),
    ] = None,
) -> None:
    """Generate an interactive 3D HTML visualization of detections."""
    import webbrowser

    from pointcloud_detect.report3d import main as write_report

    out = str(output_dir) if output_dir else "./output"
    try:
        write_report(["visualize", str(input_file), str(detections), out])
    except SystemExit as e:
        if e.code:
            raise typer.Exit(1) from None

    # Open in default browser
    out_path = Path(out) / f"{input_file.stem}_3d.html"
    if out_path.exists():
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    app()
