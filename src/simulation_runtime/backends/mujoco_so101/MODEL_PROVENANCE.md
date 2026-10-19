# SO-101 model provenance

- Upstream repository: `google-deepmind/mujoco_menagerie`
- Model: `robotstudio_so101`
- Model revision (`oid`): `76a095b3fec789ca460f4b8ceca66d11ba96040c`
- Robot description source: The Robot Studio SO-ARM100, `Simulation/SO101`
  (`so101_new_calib.xml`, upstream commit `aec17bbc256d1a7342d53aaa4950595d4c30b40d`;
  see the upstream `README.md` and `CHANGELOG.md` for derivation history).
- License: Apache-2.0; full text in `assets/robotstudio_so101/LICENSE`.

Vendored files:

```
assets/robotstudio_so101/
├── LICENSE
├── README.md
├── CHANGELOG.md
├── so101.xml
├── scene.xml          (upstream variant, unused)
├── scene_box.xml      (upstream variant, unused)
└── assets/*.stl
```

The vendored model files are unmodified. The `pick_cube` scene used by this
service is assembled at runtime by `task.py::_add_scene`, which appends a
table, target pad, cube, camera target, top camera and lighting to a parsed
copy of `so101.xml`.
