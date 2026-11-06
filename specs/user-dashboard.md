# User dashboard

Approved design: one server on port 8000, a role chooser at `/`, Developer at `/dev`, and a light, natural User interface at `/user`.

User tabs: Lamp (relative-angle aiming with hidden calibration), Microphone (five-second captures, saved playback, real waveforms and unverified animal candidates), BirdCast (state selection, manual refresh, metrics and history).

Aiming is shared across clients, pauses automatic lighting, requires confirmation to take over Developer control, expires after five minutes without adjustment, and supports Extend and Return to automatic. Developer mode changes revoke aiming. Restart defaults remain Normal/Auto.

Preserve calibration rendering, recordings, analysis, sensor collection, focus and playback. Respect reduced motion, keyboard accessibility, and mobile layouts. Role selection is not authentication.

Verification: routing, HTTP contracts, concurrent aiming and deadline behavior, hardware failures, recordings and analysis availability, migration history filtering and gaps, browser accessibility and responsiveness, physical beam movement.

Outstanding implementation validation: browser and physical hardware checks.
