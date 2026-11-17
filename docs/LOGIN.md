# How to log into the Gemini browser

The proxy works **without a Google API key**: it drives a real Firefox that is
logged into Google, and reads the session from that browser. So one-time setup
is: **log into the hidden browser window**.

## The problem

The browser that the proxy controls runs on a **headless virtual display**
(`Xvfb :2`) — it has no visible window. You cannot just look at it. That's why
the proxy ships `scripts/gemini-login.sh`: it starts the same Xvfb + Firefox and
shares the screen over **VNC**, so you connect from any VNC client, sign in, and
the cookies land in the profile the proxy reads.

## One-time login (VNC)

Install `x11vnc` (and a VNC client on your side):

```bash
apk add xvfb fluxbox xdotool py3-atspi x11vnc
```

Run the login helper **as the same user** that will run the proxy:

```bash
./scripts/gemini-login.sh
```

It prints a VNC address like:

```
 host   : <this machine's IP>
 port   : 5902    (display :2)
 password: none
```

If you are **on another machine**, tunnel first, then open the VNC client to
localhost:

```bash
ssh -L 5902:localhost:5902 user@<proxy-host>
# then connect your VNC client to localhost:5902
```

In the window:

1. It should already be on `https://accounts.google.com`.
2. Sign in with the Google account that should serve Gemini.
3. You can close the VNC client once the account is in.

Back in the shell, press **Enter**. The script stops VNC/Firefox. From now on
the proxy can read the cookies from `~/.mozilla/firefox/gemini-pump.default`.

> The script also stops `hermes-gemini-dom-watcher` first so its idle-teardown
> doesn't kill the browser mid-login, and restarts it when you're done.

## Check it worked

```bash
# the proxy should see the session:
curl -s http://127.0.0.1:9658/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-web-flash","messages":[{"role":"user","content":"hi"}]}'
```

If you get a refusal like "not logged in", re-run the login script.

## When cookies expire

Google sessions expire — the symptom is the proxy answering with a soft-refusal
("something went wrong") or `UNAUTHENTICATED`. Re-run `./scripts/gemini-login.sh`
to refresh the session. Google may also ban a session that's hammered route
limits — the rate limiter exists exactly to prevent that.

## Alternative: log in inside a normal (non-hidden) Firefox

If you prefer, log into Google in any normal Firefox window using the *same
profile path* the proxy uses (`FIREFOX_PROFILE`/`GEMINI_PUMP_PROFILE` — by
default the `gemini-pump.default` profile). The cookies are just in that
profile's `cookies.sqlite`; the proxy reads them from wherever the profile
lives. The VNC script is just the convenient way when the profile is headless.