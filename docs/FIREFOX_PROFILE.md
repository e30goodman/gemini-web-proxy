# Firefox profile diet (optional, recommended)

The bridge drives one Gemini tab in a dedicated Firefox profile
(`GEMINI_PUMP_PROFILE`). Put these lines into that profile's `user.js` to cut
memory: one content process, no auxiliary network / media / extension
processes, small caches. Measured on an ARM phone server: 10 -> 7 Firefox
processes, roughly 100 MB less, Gemini login, paste, image upload and the
accessibility tree keep working.

```js
// one content process, no isolation extras
user_pref("fission.autostart", false);
user_pref("dom.ipc.processCount", 1);
user_pref("dom.ipc.processCount.webIsolated", 1);
user_pref("dom.ipc.processPrelaunch.enabled", false);
user_pref("dom.ipc.keepProcessesAlive.web", 0);
// no auxiliary processes (Gemini needs no media/extension isolation)
user_pref("network.process.enabled", false);
user_pref("media.rdd-process.enabled", false);
user_pref("media.utility-process.enabled", false);
user_pref("extensions.webextensions.remote", false);
user_pref("browser.tabs.remote.separatePrivilegedContentProcess", false);
user_pref("browser.tabs.remote.separatePrivilegedMozillaWebContentProcess", false);
// small caches
user_pref("browser.cache.memory.enable", false);
user_pref("browser.cache.memory.capacity", 16384);
user_pref("media.memory_cache_max_size", 16384);
user_pref("image.mem.surfacecache.max_size_kb", 65536);
user_pref("browser.sessionhistory.max_total_viewers", 0);
user_pref("browser.sessionhistory.max_entries", 5);
user_pref("browser.tabs.unloadOnLowMemory", true);
user_pref("browser.sessionstore.interval", 600000);
```

On top of that the bridge can start Firefox in a systemd scope with a soft
memory cap (`GEMINI_FF_MEMORY_HIGH`, e.g. `700M`; `0` = off).
