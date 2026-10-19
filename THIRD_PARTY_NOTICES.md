# Third-party notices

This repository's own source license is not yet decided. The following
third-party material is included or required at runtime.

## The Robot Studio SO-101 model

- Path: `src/simulation_runtime/backends/mujoco_so101/assets/robotstudio_so101/`
- Upstream: [google-deepmind/mujoco_menagerie](https://github.com/google-deepmind/mujoco_menagerie),
  package `robotstudio_so101`, derived from
  [TheRobotStudio/SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100).
- License: Apache License 2.0. The complete license text is preserved at
  `assets/robotstudio_so101/LICENSE`.
- Provenance and pinned model revision: see
  `src/simulation_runtime/backends/mujoco_so101/MODEL_PROVENANCE.md`.
- The MJCF and STL files are vendored verbatim. The benchmark table, cube,
  target, camera and lighting are added at load time by
  `backends/mujoco_so101/task.py` without modifying the upstream files.

## Runtime dependencies

| Package | License | Use |
| --- | --- | --- |
| [MuJoCo](https://github.com/google-deepmind/mujoco) | Apache-2.0 | physics and rendering |
| [NumPy](https://numpy.org/) | BSD-3-Clause | arrays, IK math |
| [FastAPI](https://fastapi.tiangolo.com/) | MIT | HTTP service |
| [Starlette](https://www.starlette.io/) | BSD-3-Clause | ASGI toolkit used by FastAPI |
| [Pydantic](https://docs.pydantic.dev/) | MIT | request models |
| [Uvicorn](https://www.uvicorn.org/) | BSD-3-Clause | ASGI server |

## Development dependencies

| Package | License | Use |
| --- | --- | --- |
| [httpx](https://www.python-httpx.org/) | BSD-3-Clause | FastAPI test client |
| [pytest](https://pytest.org/) | MIT | test runner |

Dependencies are installed from their upstream distributions and are not
vendored into this repository.
