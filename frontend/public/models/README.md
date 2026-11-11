# Robot models

The viewer's robot models are baked by `frontend/scripts/bake_robot_models.py`: each URDF's
visual meshes are placed by its joint chain in a fixed pose and decimated, one mesh per URDF
part. They are not covered by this repository's Apache-2.0 license; each keeps the terms of
the meshes it was baked from.

| File | Baked from | License of the source |
|---|---|---|
| `spot.glb` | The Spot visual meshes in `spot_tools_ros/meshes/visual` of [robustrobotics/spot_tools](https://github.com/robustrobotics/spot_tools), standing pose | That repository is MIT-licensed (Copyright 2025 Robust Robotics Group); `spot_tools_ros` declares no license of its own. The meshes follow Boston Dynamics' Spot robot model, so Boston Dynamics' terms for their Spot description may also apply. |
| `husky.glb` | Clearpath Robotics' Husky A200 meshes from `clearpath_platform_description` ([clearpath_common](https://github.com/clearpathrobotics/clearpath_common)) | BSD-3-Clause, Copyright Clearpath Robotics, Inc. |

Spot is a trademark of Boston Dynamics; Husky is a trademark of Clearpath Robotics. Neither
company is affiliated with this project.
