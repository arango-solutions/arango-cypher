# ServiceMaker

> GitHub repository: <https://github.com/arangodb/servicemaker>

A tool to wrap Python projects as Docker services.

## Features

- Takes an existing Python project and creates a Docker image.
- Interactive prompts for missing configuration.
- Uses pre-built base images with Python and common libraries pre-installed.
- Customizable base image and entrypoint.
- Optional image push to Docker registry.
- Optional creation of `tar.gz` archive with project files and virtual environment changes.

## Installation

Build the project:

```bash
cargo build --release
```

The binary will be available at `target/release/servicemaker`.

## Usage

With all options specified:

```bash
servicemaker \
  --name myproject \
  --project-home /path/to/python/project \
  --base-image arangodb/py13base:latest \
  --port 8080 \
  --image-name myregistry/myproject:latest \
  --entrypoint main.py \
  --push
```

Interactive mode (prompts for missing options):

```bash
servicemaker
```

## Command-line Options

| Option           | Description                                                                                                                                   |
|------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| `--name`         | Name of the project (optional, will prompt if not provided).                                                                                  |
| `--project-home` | Path to the folder containing the Python project (optional, will prompt if not provided).                                                     |
| `--base-image`   | Base Docker image (default: `arangodb/py13base:latest`).                                                                                      |
| `--port`         | Exposed port number (optional, will prompt if not provided).                                                                                  |
| `--image-name`   | Docker image name to push (optional, will prompt if not provided). Can include registry prefix (e.g., `myregistry.com/myproject:latest`).      |
| `--push`         | Whether to push the image (default: `false`).                                                                                                 |
| `--entrypoint`   | Name of the Python script to run relative to project home (optional, will prompt if not provided).                                            |
| `--make-tar-gz`  | Whether to create a `tar.gz` archive with project files and virtual environment changes (default: `false`). See *The `--make-tar-gz` Option*. |

## How it Works

1. Reads command-line arguments or prompts for missing values.
2. Validates that the project home directory exists.
3. Creates a temporary directory in the current directory (e.g., `./servicemaker-<projectname>-<pid>`).
4. Modifies the Dockerfile template with:
   - Custom base image.
   - `EXPOSE` directive for the specified port.
   - Custom entrypoint script.
5. Copies the Dockerfile to the temporary directory.
6. Recursively copies the Python project to the `project/` subdirectory.
7. Runs `docker build` to create the image.
8. Optionally runs `docker push` if `--push` is specified.
9. Optionally creates a `tar.gz` archive if `--make-tar-gz` is specified.
10. Generates a Helm chart for Kubernetes deployment.

## Base Images

ServiceMaker uses pre-built base images that provide a robust foundation for Python services. These base images:

- Start with `debian:trixie-slim` for a minimal, secure base.
- Include a non-root user `user` with the `uv` package manager pre-installed.
- Have a specific Python version (e.g., Python 3.13) pre-installed via `uv`.
- Create a virtual environment called `the_venv` in the user's home directory (`/home/user/the_venv`).
- Pre-install common libraries into the virtual environment.
- Include a SHA256 checksum file (`sums_sha256`) of all files in the virtual environment for change tracking.

The base image setup ensures that:

- Common dependencies are pre-installed and cached in the base image.
- Only new dependencies added by your project need to be installed during build.
- The base image can be pre-scanned for security vulnerabilities.
- Build times are faster due to layer caching.

### Current Base Images

The following base images are available in the `baseimages/` directory:

- **`arangodb/py13base:latest`** (default)
  - Python 3.13.
  - Pre-installed packages: `python-arango`, `phenolrs`, `networkx`.
- **`arangodb/py12base:latest`**
  - Python 3.12.
  - Pre-installed packages: `python-arango`, `phenolrs`, `networkx`.
- **`arangodb/py13cugraph:latest`**
  - Python 3.13.
  - Pre-installed packages: `python-arango`, `phenolrs`, `networkx`, `cugraph-cu12`.
  - Uses NVIDIA PyPI index for CUDA-accelerated graph libraries.

### Building Base Images

Base images are defined in the `baseimages/` directory. Each base image has its own Dockerfile (e.g., `Dockerfile.py13base`). To build all base images:

```bash
cd baseimages
make build
```

This will build all images listed in `imagelist.txt`. To build a specific base image:

```bash
cd baseimages
docker build -f Dockerfile.py13base -t arangodb/py13base .
```

To push base images to a registry:

```bash
cd baseimages
make push
```

You can create additional base images for different Python versions or with different pre-installed libraries by:

1. Creating a new Dockerfile in the `baseimages/` directory (e.g., `Dockerfile.py14base`).
2. Adding the image name to `imagelist.txt`.
3. Building with `make build`.

## Dockerfile Template

The tool uses the Dockerfile template in the project root. The template:

- Uses the specified base image (which already has `uv` and Python installed).
- Copies the project to `/home/user/project`.
- Activates the existing virtual environment (`the_venv`) from the base image.
- Runs `uv sync --active` to install only additional dependencies not already in the base image.
- Executes the specified entrypoint script.

This approach ensures that:

- Dependencies are installed efficiently (only new ones are added).
- The final image shares layers with the base image (faster builds and smaller images).
- Base images can be pre-scanned for security vulnerabilities.
- The virtual environment works seamlessly with modern Python tooling.

## Python Version

The Python version is determined by the base image you select. The default base image (`arangodb/py13base:latest`) includes Python 3.13.

You should declare the Python version in your project's `pyproject.toml` file to match the base image's Python version. The `uv` package manager will use the Python version from the base image's virtual environment.

For example, to use Python 3.13 (matching the default base image):

```toml
requires-python = "==3.13.*"
```

When using `uv sync --active`, the tool installs dependencies into the existing virtual environment from the base image, ensuring compatibility with the pre-installed Python version.

## The `--make-tar-gz` Option

The `--make-tar-gz` option creates a portable archive (`project.tar.gz`) containing your project files and any virtual environment changes made during the Docker build process.

### What it does

When `--make-tar-gz` is enabled, ServiceMaker:

1. After building the Docker image, runs a container using that image.
2. Executes the `zipper.sh` script inside the container.
3. Creates a `tar.gz` archive containing:
   - **`the_venv/`** — all new files added to the virtual environment during the build (dependencies installed by your project).
   - **`entrypoint`** — a symlink to your entrypoint script.
   - **Your project directory** — all your project files.

The archive is saved to the temporary directory (e.g., `./servicemaker-<projectname>-<pid>/project.tar.gz`).

### Use cases

- **Portable deployment** — deploy your project without needing Docker.
- **Development environments** — extract and run the project in a non-containerized environment.
- **Backup** — archive your project with all its dependencies.
- **CI/CD pipelines** — use the archive in environments where Docker images aren't available.

### Example usage

```bash
servicemaker \
  --name myproject \
  --project-home /path/to/python/project \
  --base-image arangodb/py13base:latest \
  --port 8080 \
  --image-name myregistry/myproject:latest \
  --entrypoint main.py \
  --make-tar-gz
```

The `project.tar.gz` file will be created in the temporary directory after the Docker image is built.

## Running the Derived Docker Image

After ServiceMaker builds your Docker image, you can run it using standard Docker commands.

### Basic usage

```bash
docker run -p <host-port>:<container-port> <image-name>
```

For example, if your image exposes port `8080`:

```bash
docker run -p 8080:8080 myregistry/myproject:latest
```

### With environment variables

```bash
docker run -p 8080:8080 \
  -e ENV_VAR1=value1 \
  -e ENV_VAR2=value2 \
  myregistry/myproject:latest
```

### In detached mode

```bash
docker run -d -p 8080:8080 --name myproject myregistry/myproject:latest
```

### View logs

```bash
docker logs myproject
# or for detached containers
docker logs -f myproject
```

### Stop and remove

```bash
docker stop myproject
docker rm myproject
```

## Running the `tar.gz` Archive

The `project.tar.gz` archive created with `--make-tar-gz` can be extracted and run on any system with Python installed (matching the Python version from your base image).

### Extracting the archive

```bash
# Extract the archive
tar -xzf project.tar.gz

# This will create:
# - the_venv/          (virtual environment with dependencies)
# - entrypoint         (symlink to your entrypoint script)
# - <project-directory>/   (your project files)
```

### Running the project

The archive contains a virtual environment with all dependencies. To run your project:

```bash
# Activate the virtual environment
source the_venv/bin/activate

# Set PYTHONPATH to include the virtual environment's site-packages
export PYTHONPATH=$(pwd)/the_venv/lib/python3.13/site-packages:$PYTHONPATH

# Run your entrypoint script
python entrypoint
# or directly
python <project-directory>/main.py
```

### Using `uv` (recommended)

If `uv` is installed on the host system:

```bash
# Extract the archive
tar -xzf project.tar.gz

# Navigate to your project directory
cd <project-directory>

# Run with uv (it will use the existing virtual environment)
uv run --active main.py
```

### Notes

- The Python version must match the base image version (e.g., Python 3.13 for `py13base`).
- The archive includes only the dependencies added by your project, not the base image's pre-installed packages.
- For full functionality, ensure the base image's pre-installed packages are available or install them separately.
- The `entrypoint` symlink points to your entrypoint script relative to the project directory.

## Notes

- The temporary directory is left behind after execution for inspection.
- The project directory must contain a valid Python project with a `pyproject.toml` or `requirements.txt` for `uv` to work properly.
- Docker must be installed and accessible for this tool to work.
- Base images must be available locally or pulled from a registry before building.
- The base image's Python version should match the Python version requirement in your `pyproject.toml`.
