# Domain glossary

- **Configuration backup:** A portable file containing lighting and website settings; excludes sensor readings, analysis results, and audio recordings.
- **Settings persistence:** Automatic server-side saving that retains configuration across server restarts and device reboots.
- **Direction calibration:** Spiral spacing (`alignment`), beam width, and LED offset used to align and aim the lamp's vertical light column. The displayed angle is derived from spacing and offset.

- **User dashboard:** Simplified public interface at `/user` for aiming, audio clips, and migration.
- **Developer dashboard:** Existing configuration interface at `/dev`.
- **User aiming:** Shared temporary lamp control with a server-owned five-minute inactivity deadline.
