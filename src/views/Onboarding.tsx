import { useEffect, useState } from "react";
import { ProviderPreset, Settings, getProviders, getSettings, putSettings, setVoice } from "../api";
import type { Connection } from "../shell";

type Step = "welcome" | "you" | "ai" | "control" | "voice" | "done";
const ORDER: Step[] = ["welcome", "you", "ai", "control", "voice", "done"];

/** First-run setup for a new install: who you are, the AI key, what JARVIS may do, voice. Shown once (and
 *  again from Settings > System). Every step can be skipped; everything is also in Settings. */
export default function Onboarding({ conn, onChanged }: { conn: Connection | null; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step>("welcome");
  const [s, setS] = useState<Settings | null>(null);
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [name, setName] = useState("");
  const [address, setAddress] = useState("sir");
  const [provider, setProvider] = useState("gemini");
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [full, setFull] = useState(false);
  const [voiceOn, setVoiceOn] = useState(true);
  const [briefing, setBriefing] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!conn) return;
    Promise.all([getSettings(conn), getProviders(conn)])
      .then(([fresh, ps]) => {
        setS(fresh);
        setPresets(ps);
        setName(fresh.user_name);
        setAddress(fresh.address || "sir");
        setProvider(fresh.provider);
        setBaseUrl(fresh.base_url);
        setFull(!!fresh.full_control);
        setBriefing(fresh.startup_briefing);
        if (!fresh.onboarded) setOpen(true);
      })
      .catch(() => {});
    const again = () => {
      setStep("welcome");
      setOpen(true);
    };
    window.addEventListener("jarvis:onboarding", again);
    return () => window.removeEventListener("jarvis:onboarding", again);
  }, [conn]);

  if (!open || !conn || !s) return null;
  const preset = presets.find((p) => p.id === provider);
  const next = () => setStep(ORDER[Math.min(ORDER.indexOf(step) + 1, ORDER.length - 1)]);
  const back = () => setStep(ORDER[Math.max(ORDER.indexOf(step) - 1, 0)]);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
      next();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const finish = async () => {
    await putSettings(conn, { onboarded: true }).catch(() => {});
    onChanged();
    setOpen(false);
  };

  return (
    <div className="onboard-backdrop">
      <div className="onboard glass" role="dialog" aria-label="Set up JARVIS">
        <div className="onboard-steps">{ORDER.map((x) => <i key={x} className={x === step ? "on" : ORDER.indexOf(x) < ORDER.indexOf(step) ? "done" : ""} />)}</div>

        {step === "welcome" && (
          <>
            <h2>Hello. I'm JARVIS.</h2>
            <p>I live on this PC: I talk with you, remember what matters to you, and use your apps, files and the web when you ask. A few quick choices and we're ready. You can change all of it later in Settings.</p>
            <div className="field-row"><button onClick={next}>LET'S GO</button><button className="ghost" onClick={finish}>SKIP SETUP</button></div>
          </>
        )}

        {step === "you" && (
          <>
            <h2>Who am I working for?</h2>
            <label><span>Your name</span><input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></label>
            <label><span>What should I call you?</span><input value={address} maxLength={40} placeholder="sir" onChange={(e) => setAddress(e.target.value)} /></label>
            <div className="field-row">
              <button className="ghost" onClick={back}>BACK</button>
              <button disabled={busy} onClick={() => run(() => putSettings(conn, { user_name: name.trim(), address: address.trim() || "sir" }))}>NEXT</button>
            </div>
          </>
        )}

        {step === "ai" && (
          <>
            <h2>My brain</h2>
            <p className="dim small">I need an AI service to think with. Bring your own key (Google Gemini has a free tier at aistudio.google.com), or a local or self-hosted one. It's encrypted and stays on this PC.</p>
            <label>
              <span>Service</span>
              <select value={provider} onChange={(e) => { setProvider(e.target.value); setBaseUrl(presets.find((p) => p.id === e.target.value)?.base_url ?? ""); }}>
                {presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            {preset && !preset.needs_key && (
              <label><span>Address</span><input value={baseUrl} spellCheck={false} placeholder="http://127.0.0.1:…/v1" onChange={(e) => setBaseUrl(e.target.value)} /></label>
            )}
            <label>
              <span>API key{preset && !preset.needs_key ? " (if it needs one)" : ""}</span>
              <input type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)}
                placeholder={s.has_api_key && provider === s.provider ? "A key is already saved" : preset?.key_hint ?? ""} />
            </label>
            <div className="field-row">
              <button className="ghost" onClick={back}>BACK</button>
              <button className="ghost" onClick={next}>LATER</button>
              <button disabled={busy || (!!preset?.needs_key && !key.trim() && !(s.has_api_key && provider === s.provider))}
                onClick={() => run(() => putSettings(conn, { provider, ...(key.trim() ? { api_key: key.trim() } : {}), ...(preset && !preset.needs_key ? { base_url: baseUrl.trim() } : {}) }))}>
                {busy ? "CONNECTING…" : "CONNECT"}
              </button>
            </div>
          </>
        )}

        {step === "control" && (
          <>
            <h2>What may I do on this PC?</h2>
            <label className="choice">
              <input type="radio" name="control" checked={!full} onChange={() => setFull(false)} />
              <span><strong>Ask me first</strong> before changing anything: files, commands, apps. Best on a shared or work PC.</span>
            </label>
            <label className="choice">
              <input type="radio" name="control" checked={full} onChange={() => setFull(true)} />
              <span><strong>Full control</strong>: it's my own PC, just do it. Installing software, system changes, power and sending or paying still ask.</span>
            </label>
            <p className="dim small">Fine-tune each kind of action later in Settings › PC control & safety.</p>
            <div className="field-row">
              <button className="ghost" onClick={back}>BACK</button>
              <button disabled={busy} onClick={() => run(() => putSettings(conn, { full_control: full }))}>NEXT</button>
            </div>
          </>
        )}

        {step === "voice" && (
          <>
            <h2>Talking</h2>
            <label className="switch"><input type="checkbox" checked={voiceOn} onChange={(e) => setVoiceOn(e.target.checked)} /><span>Listen for my voice: say "Jarvis" and your request</span></label>
            <label className="switch"><input type="checkbox" checked={briefing} onChange={(e) => setBriefing(e.target.checked)} /><span>Give me a spoken briefing when JARVIS starts</span></label>
            <p className="dim small">Tip: in Settings › Voice, "Teach JARVIS my voice" makes me answer only you.</p>
            <div className="field-row">
              <button className="ghost" onClick={back}>BACK</button>
              <button disabled={busy} onClick={() => run(async () => {
                await putSettings(conn, { startup_briefing: briefing });
                await setVoice(conn, voiceOn).catch(() => null);  // voice files may still be downloading; Settings shows that
              })}>NEXT</button>
            </div>
          </>
        )}

        {step === "done" && (
          <>
            <h2>All set{name.trim() ? `, ${name.trim()}` : ""}.</h2>
            <p>Talk to me or type. Ctrl+Alt+Shift+S stops anything I'm doing, from any app.</p>
            <div className="field-row"><button onClick={finish}>START</button></div>
          </>
        )}
        {error && <p className="bad small">{error}</p>}
      </div>
    </div>
  );
}
