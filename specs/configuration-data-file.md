# Persistent website settings and configuration backups

Status: Awaiting final user approval. Implementation is a separate task.

## Problem

Lighting and direction settings currently reset when the server restarts. The user primarily wants settings to survive server restarts and device reboots, with portable backups for later restoration.

## Solution and goals

Automatically save shared lighting preferences on the server, retain display choices per browser, and provide readable JSON backup download/upload under Configuration → Settings & backup.

## Scope and terminology

Shared settings include direction spacing (`alignment`), beam width, offset, manual brightness, calibration brightness if independently represented, and selected light color. Preset buttons save the resulting values, not a separate preset identity. The direction angle is derived from spacing and offset, not independently authoritative.

Browser display preferences include BirdCast region, graph metric and time range for each graph, and selected animal. Preserve existing URL/navigation behavior; last page, open menus, focus, playback position, and transient interactions are not configuration.

Operating state is distinct from saved preferences. Station mode, light on/off/auto/test mode, active captures, recording scheduler enabled state, sensor values, controller acknowledgements, pending commands, and diagnostic logs are transient and are not restored.

Read-only hardware information and fixed code/environment parameters are not editable website settings. This feature does not add editing or backup of WLED firmware configuration, serial configuration, thresholds, sampling intervals, secrets, or deployment configuration.

## Intended behavior

### Automatic saving and startup

- Persist each accepted change to shared settings automatically, without a Save button. A completed successful settings request means the values are durably saved. Serialize concurrent changes so a stale write cannot overwrite newer accepted state.
- Use a dedicated versioned JSON configuration file under the repository's ignored runtime directory. Do not silently load the stale `.runtime/lamp_config.json` from the reverted implementation.
- On startup, load saved preferences before controls become available. Always start in Normal station mode with automatic lighting behavior and microphone scheduling stopped, as today. Retain calibration and manual preferences for subsequent use; do not activate calibration or manual output merely because they were saved.
- Entering Direction Config must reuse the saved spacing, beam width, and offset. Manual lighting controls must show the saved brightness and color when available in their permitted mode.
- Saving preferences must not require a working light controller. Distinguish saved configuration from hardware delivery or acknowledgement; report controller failures through existing status behavior.
- Replace saved files atomically. If saving fails, clearly report that the change was not saved and retain the previous accepted configuration. Do not show a durable-save success.
- With no settings file, use existing defaults. With corrupt or unsupported stored configuration, preserve the original file, report the problem visibly, and run with existing defaults without silently overwriting the file. Explicit confirmed restore may replace it.
- Browser display choices survive reload and server reboot in that browser. Visitors do not overwrite each other's display preferences. Unavailable browser storage must produce a clear persistence warning while allowing the dashboard to operate.
- Invalid or obsolete browser selections fall back to the existing valid default; an animal absent from the current library uses the existing available-animal fallback.

### Backup download

- Download one human-readable JSON file containing a format identifier, schema version, export timestamp, all shared preferences, and the exporting browser's display preferences.
- Export a coherent snapshot of accepted settings, independent of current sensor/controller availability. Do not export pending edits or measurements.
- Use a descriptive timestamped `.json` filename.

### Backup upload and restoration

- Validate the entire file before changing anything. Reject malformed JSON, wrong types, out-of-range values, missing required fields, unknown configuration fields, unsupported versions, and inconsistent calibration values using the same constraints as normal settings changes.
- Show a preview of current and proposed values, explicitly identifying shared lighting preferences versus this browser's preferences. Unchanged fields may be summarized.
- Require explicit confirmation. Cancel or failed validation changes nothing.
- Restore the complete settings snapshot; partial section restoration is outside scope. Use the same persistence guarantees as ordinary edits. If shared settings changed after preview, require an updated preview before confirmation can succeed.
- Successful restoration saves shared settings and updates only the importing browser's display preferences. Explain any browser-storage failure separately; do not claim both locations were saved if only server persistence succeeded.
- Restore does not change station mode or start recording. If the current mode uses affected lighting settings, apply them using existing mode rules. Calibration settings restored outside Direction Config remain dormant until that mode is entered. Report hardware delivery failures separately from successful saving.
- Browser refreshes must continue to preserve audio elements, focus, and open controls as existing behavior requires.

### Access and controls

- Keep current public dashboard access policy. Backup/restore is available to the same visitors who can already change settings; do not add authentication in this task.
- Clearly distinguish automatic saving from downloading a backup, show save/restore errors, and explain startup behavior on the settings page.
- Preserve spacing sliders and presets, beam width, the separate Output → Direction dial and nudges, and WLED grouping/spacing/offset rendering. Do not reintroduce explicit pixel rendering or the reverted request queue.

## Non-goals

Sensor logs, audio recordings, animal results, full-device backups, WLED firmware backups, new hardware controls, automatic resumption of runtime modes, and changes to physical rendering are excluded.

## Decisions and rationale

- User selected automatic persistence plus download/upload, with reboot survival the priority.
- User limited scope to lighting and website settings, excluding sensor readings and recordings.
- User accepted Normal startup with recording stopped, per-browser display choices, and preview/confirmation for full restoration.
- JSON is readable, supports validation, and fits structured settings. Version the format to reject incompatible backups clearly.
- Proposed review details: include current browser preferences in portable backups, keep runtime modes transient, and use Configuration → Settings & backup as the interface location.

## Verification

- Change every shared field through normal controls; restart the actual service and verify restored values through API and dashboard. Confirm Normal/Auto startup and stopped recording schedule.
- Test spacing 51, width 1, offset 0, plus a second non-default valid calibration with a nonzero offset. Verify both survive restart and are reused on entering Direction Config.
- Confirm calibration sliders, presets, width, dial, and nudges retain existing behavior and mode restrictions. Visually verify physical alignment before claiming it; mocks and acknowledgements cannot prove alignment.
- Refresh two independent browsers and restart the service: shared preferences agree; display choices remain independent. Test unavailable storage and obsolete selected animals/regions.
- Download, change settings, preview, cancel, then confirm restoration; verify saved values after another restart and verify only the importing browser's views change.
- Invalid, incomplete, future-version, and out-of-range files must change no configuration. Test corrupt startup files, missing files, failed writes, concurrent edits, changed settings during preview, and disconnected controller behavior.
- Confirm backups contain no readings, recordings, analysis data, secrets, or transient hardware state. Verify existing collection/history behavior and audio/control preservation.
- Run relevant service, lighting, frontend, and browser checks, updating mirrored service tests where applicable. Report the known baseline DOM-mock failure separately if encountered.
- After implementation, restart `bioglow.service`, verify `/v1/latest`, and verify real restart restoration without launching another server.

## References

- `AGENTS.md`: station-mode behavior, rollback/persistence constraints, deployment, and hardware verification.
- `CONTEXT.md`: configuration terminology.
- `birdcast_service.py`, `lighting_control.py`: settings and rendering behavior.
- `web/dashboard.js`, `web/index.html`: controls and display choices.
- `collection_schedule.py`: recording startup behavior.
