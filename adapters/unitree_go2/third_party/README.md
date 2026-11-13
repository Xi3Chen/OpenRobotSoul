# Unitree Go2 third-party workspace

Place the selected Unitree Go2 Python SDK, ROS 2 driver, or a pinned source checkout under `sdk/`. All robot-specific project code and dependencies belong in `adapters/unitree_go2/`.

Before writing the project bridge, record the exact SDK/driver version, license, supported Go2 firmware, transport/control ownership process, state feedback, stop behavior, and disconnect behavior. Keep credentials and robot network details out of this directory and out of Git.

The repository intentionally does not assume a particular vendor SDK API. Implement the `UnitreeGo2Driver` protocol in this robot module and translate vendor messages into OpenRobotSoul's typed `Observation`, `Command`, `Feedback`, and verified `CapabilitySpec` contracts.

The development profile in `profiles/unitree-go2-development.json` declares the SportClient actions wrapped by the bridge. Profile limits are the maximum allowed bounds. `Damp` and low-level motor commands stay out. SDK acceptance is not hardware verification of pose completion, disconnect behavior, or emergency stop.
