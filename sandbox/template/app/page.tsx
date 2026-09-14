"use client";

// The placeholder home page, shown until the app's own UI is written. Replace this whole file with
// the app's home page: nothing else in the app depends on it.

import { useEffect, useState } from "react";

const TURN_ASK_TYPE = "bial:turn-ask";
const TURN_TYPE = "bial:turn";
const ASK_EVERY_MS = 2000;

/** The portal framing this page: the browser's own record first, then the injected config. */
function framingOrigin(): string | null {
  const framer = window.location.ancestorOrigins?.[0];
  if (framer) return framer;
  const published = window.__BIAL_CONFIG?.portalOrigin;
  return published ? published : null;
}

export default function Home() {
  const [building, setBuilding] = useState(false);
  const [framed, setFramed] = useState(true);

  useEffect(() => {
    if (window.parent === window) {
      setFramed(false);
      return;
    }
    const origin = framingOrigin();
    if (!origin) return;
    // Only the framing window, at the origin it was proven to have, is heard or asked — never '*'.
    const onMessage = (event: MessageEvent) => {
      if (event.source !== window.parent || event.origin !== origin) return;
      const data: unknown = event.data;
      if (typeof data !== "object" || data === null || !("type" in data) || data.type !== TURN_TYPE) return;
      if (!("running" in data) || typeof data.running !== "boolean") return;
      setBuilding(data.running);
    };
    const ask = () => window.parent.postMessage({ type: TURN_ASK_TYPE }, origin);
    window.addEventListener("message", onMessage);
    ask();
    const timer = window.setInterval(ask, ASK_EVERY_MS);
    return () => {
      window.removeEventListener("message", onMessage);
      window.clearInterval(timer);
    };
  }, []);

  const title = building ? "Building your app…" : "Nothing here yet";
  const line = building
    ? "It will appear here as soon as the first screen is ready."
    : `Describe what you need${framed ? " on the left" : ""} and I will build it. You will see it appear here as it goes.`;

  return (
    <main className="starter" data-state={building ? "building" : "waiting"}>
      <style href="bial-starter" precedence="default">
        {STARTER_CSS}
      </style>
      <div className="starter-stage" aria-hidden="true">
        <div className="starter-window" />
        <span className="starter-piece starter-topbar" />
        <span className="starter-piece starter-side" />
        <span className="starter-piece starter-heading" />
        <span className="starter-piece starter-button" />
        <span className="starter-piece starter-avatar" />
        <span className="starter-piece starter-chart">
          <span className="starter-bar" />
          <span className="starter-bar" />
          <span className="starter-bar" />
          <span className="starter-bar" />
          <span className="starter-bar" />
        </span>
        <span className="starter-piece starter-stat" />
        <span className="starter-piece starter-rows" />
      </div>
      <div className="starter-copy" aria-live="polite">
        <h1 className="starter-title">{title}</h1>
        <p className="starter-line">{line}</p>
      </div>
    </main>
  );
}

// The words are painted from the first frame and only the decoration animates: the portal reveals
// the preview only once this page shows visible text, and a faded-in headline would delay that.
const STARTER_CSS = `
.starter {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 28px;
  padding: 32px 24px;
  text-align: center;
  color: #12292a;
}
.starter-copy { max-width: 430px; }
.starter-title { margin: 0 0 8px; font-size: 22px; line-height: 1.25; font-weight: 700; letter-spacing: -0.018em; text-wrap: balance; }
.starter-line { margin: 0; font-size: 15px; line-height: 1.6; color: #586c6d; text-wrap: pretty; }

.starter-stage { --m: 1; position: relative; width: 320px; height: 220px; flex: none; }
.starter-window {
  position: absolute;
  inset: 0;
  background: #ffffff;
  border: 1px solid #dae4e5;
  border-radius: 14px;
  box-shadow: 0 34px 60px -38px rgba(16, 41, 42, 0.4), 0 2px 6px rgba(16, 41, 42, 0.04);
  transition: box-shadow 0.8s ease;
}
.starter-window::before {
  content: "";
  position: absolute;
  left: 14px;
  top: 11px;
  width: 36px;
  height: 8px;
  background: radial-gradient(circle at 4px 4px, #d3dedf 3.2px, transparent 3.7px) 0 0 / 13px 8px repeat-x;
}
.starter-window::after { content: ""; position: absolute; left: 0; right: 0; top: 29px; height: 1px; background: #edf2f2; }
[data-state="building"] .starter-window {
  box-shadow: 0 34px 60px -38px rgba(16, 41, 42, 0.4), 0 0 0 6px rgba(13, 115, 119, 0.07);
}

.starter-piece {
  position: absolute;
  left: var(--x);
  top: var(--y);
  width: var(--w);
  height: var(--h);
  border-radius: 7px;
  opacity: 0.8;
  box-shadow: 0 8px 18px -10px rgba(16, 41, 42, 0.3);
  transform: translate(calc(var(--sx) * var(--m)), calc(var(--sy) * var(--m))) rotate(var(--sr));
  animation: starter-bob 5.6s ease-in-out infinite;
  animation-delay: calc(var(--k) * -0.7s);
}
[data-state="building"] .starter-piece {
  box-shadow: none;
  animation: starter-assemble 6.4s infinite backwards;
  animation-delay: calc(var(--k) * 0.2s);
}
.starter-topbar { --k: 0; --x: 14px; --y: 38px; --w: 292px; --h: 18px; --sx: -40px; --sy: -58px; --sr: -8deg; background: #e8f0f1; }
.starter-side {
  --k: 1; --x: 14px; --y: 64px; --w: 64px; --h: 142px; --sx: -150px; --sy: -10px; --sr: 8deg;
  background-color: #f0f5f5;
  background-image: repeating-linear-gradient(to bottom, transparent 0 12px, #d5e2e3 12px 18px, transparent 18px 26px);
  background-size: 40px 100%;
  background-position: 12px 6px;
  background-repeat: no-repeat;
}
.starter-heading { --k: 2; --x: 90px; --y: 66px; --w: 118px; --h: 11px; --sx: -20px; --sy: -80px; --sr: 5deg; border-radius: 6px; background: #3d5657; }
.starter-button { --k: 3; --x: 246px; --y: 62px; --w: 60px; --h: 20px; --sx: 150px; --sy: -30px; --sr: -12deg; background: #0d7377; }
.starter-avatar { --k: 4; --x: 284px; --y: 40px; --w: 14px; --h: 14px; --sx: 60px; --sy: -52px; --sr: 0deg; border-radius: 50%; background: #0d7377; }
.starter-chart { --k: 5; --x: 90px; --y: 92px; --w: 134px; --h: 66px; --sx: -190px; --sy: 60px; --sr: -7deg; background: #ffffff; border: 1px solid #dfe8e9; }
.starter-stat { --k: 6; --x: 232px; --y: 92px; --w: 74px; --h: 66px; --sx: 180px; --sy: 50px; --sr: 10deg; background: #e1f0f0; }
.starter-stat::before { content: ""; position: absolute; left: 10px; top: 13px; width: 40px; height: 14px; border-radius: 4px; background: #0d7377; opacity: 0.85; }
.starter-stat::after { content: ""; position: absolute; left: 10px; top: 36px; width: 30px; height: 6px; border-radius: 3px; background: #9fc9ca; }
.starter-rows {
  --k: 7; --x: 90px; --y: 166px; --w: 216px; --h: 40px; --sx: 130px; --sy: 20px; --sr: 4deg;
  background-color: #ffffff;
  background-image: repeating-linear-gradient(to bottom, #e6eeef 0 7px, transparent 7px 16px);
  background-size: calc(100% - 12px) 100%;
  background-position: 6px 5px;
  background-repeat: no-repeat;
}

.starter-bar { position: absolute; bottom: 9px; width: 14px; border-radius: 3px 3px 0 0; transform-origin: bottom; }
.starter-bar:nth-child(1) { --b: 0; left: 12px; height: 22px; background: #0d7377; }
.starter-bar:nth-child(2) { --b: 1; left: 34px; height: 36px; background: #7fcfcb; }
.starter-bar:nth-child(3) { --b: 2; left: 56px; height: 28px; background: #0d7377; }
.starter-bar:nth-child(4) { --b: 3; left: 78px; height: 44px; background: #7fcfcb; }
.starter-bar:nth-child(5) { --b: 4; left: 100px; height: 32px; background: #0d7377; }
[data-state="building"] .starter-bar {
  animation: starter-grow 6.4s infinite backwards;
  animation-delay: calc(1s + var(--b) * 0.08s);
}

@keyframes starter-bob {
  0%, 100% { transform: translate(calc(var(--sx) * var(--m)), calc(var(--sy) * var(--m))) rotate(var(--sr)); }
  50% { transform: translate(calc(var(--sx) * var(--m)), calc(var(--sy) * var(--m) - 9px)) rotate(calc(var(--sr) * -0.5)); }
}
@keyframes starter-assemble {
  0% {
    transform: translate(calc(var(--sx) * var(--m)), calc(var(--sy) * var(--m))) rotate(var(--sr)) scale(0.9);
    opacity: 0;
    animation-timing-function: cubic-bezier(0.3, 1.4, 0.55, 1);
  }
  16% { transform: translate(0, 0) rotate(0deg) scale(1); opacity: 1; }
  72% { transform: translate(0, 0) rotate(0deg) scale(1); opacity: 1; animation-timing-function: ease-in; }
  84% { transform: translate(0, 0) rotate(0deg) scale(0.94); opacity: 0; }
  100% { transform: translate(calc(var(--sx) * var(--m)), calc(var(--sy) * var(--m))) rotate(var(--sr)) scale(0.9); opacity: 0; }
}
@keyframes starter-grow {
  0%, 13% { transform: scaleY(0.12); }
  24%, 72% { transform: scaleY(1); }
  84%, 100% { transform: scaleY(0.12); }
}

@media (max-width: 640px) {
  .starter-stage { --m: 0.5; transform: scale(0.86); margin: -14px 0; }
  .starter-title { font-size: 21px; }
}

@media (prefers-reduced-motion: reduce) {
  .starter *, .starter *::before, .starter *::after { animation: none !important; transition: none !important; }
  [data-state="building"] .starter-piece { opacity: 1; transform: none; }
}
`;
