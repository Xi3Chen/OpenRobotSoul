# Unitree Go2 third-party workspace

Place the selected Unitree Go2 Python SDK, ROS 2 driver, or a pinned source checkout under `sdk/`. All robot-specific project code and dependencies belong in `adapters/unitree_go2/`.

Before writing the project bridge, record the exact SDK/driver version, license, supported Go2 firmware, transport/control ownership process, state feedback, stop behavior, and disconnect behavior. Keep credentials and robot network details out of this directory and out of Git.

The repository intentionally does not assume a particular vendor SDK API. Implement the `UnitreeGo2Driver` protocol in this robot module and translate vendor messages into OpenRobotSoul's typed `Observation`, `Command`, `Feedback`, and verified `CapabilitySpec` contracts.

The development profile in `profiles/unitree-go2-development.json` declares the SportClient actions wrapped by the bridge. Profile limits are the maximum allowed bounds. `Damp` and low-level motor commands stay out. SDK acceptance is not hardware verification of pose completion, disconnect behavior, or emergency stop.

`unitree_webrtc_connect/` is the pinned WebRTC client used only for the Go2 speaker. Motion stays on the SDK2 DDS bridge. Set `OPENROBOTSOUL_GO2_WEBRTC_IP` to the robot `wlan0` address to register the in-process `unitree-go2` audio output when the adapter connects. `OPENROBOTSOUL_GO2_AES_KEY` is optional and is the per-device key required by Go2 firmware at or above 1.1.15. The official app cannot hold the robot WebRTC session at the same time. If that connection fails, sport control still comes up and the virtual output is not registered. Dialogue audio is a replaceable PCM stream on the negotiated audio track, not an AudioHub file upload. Importing that client also needs `packaging`, `requests`, `pycryptodome`, `curl_cffi`, `wasmtime`, and `lz4`. Its audio module imports `sounddevice`, but speaker playback does not require PortAudio.
