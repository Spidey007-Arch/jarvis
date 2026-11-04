import { useCallback, useEffect, useState } from "react";
import { CalendarView, NewEvent, addEvent, deleteEvent, getCalendar, linkCalendar, putSettings, unlinkCalendar } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;

const pad = (n: number) => String(n).padStart(2, "0");
const local = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
function nextHour(): string {
  const d = new Date();
  d.setHours(d.getHours() + 1, 0, 0, 0);
  return local(d);
}
const EMPTY = (): NewEvent => ({ title: "", start: nextHour(), end: "", all_day: false, location: "", notes: "", calendar: "jarvis", remind_minutes: null });
const dayLabel = (s: string) => new Date(s.length === 10 ? `${s}T00:00` : s).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
const clock = (s: string) => s.slice(11, 16);

/** The user's calendars in one list, adding events, and linking Google / Outlook.com calendars by their private address. */
export default function CalendarCard({ conn }: { conn: Connection | null }) {
  const [view, setView] = useState<CalendarView | null>(null);
  const [draft, setDraft] = useState<NewEvent | null>(null);
  const [link, setLink] = useState({ name: "", url: "" });
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (conn) setView(await getCalendar(conn).catch((e) => { fail(e); return null; }));
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await fn();
      setNote({ tone: "ok", text: done });
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const days: [string, CalendarView["events"]][] = [];
  for (const e of view?.events ?? []) {
    const key = e.start.slice(0, 10);
    const last = days[days.length - 1];
    if (last && last[0] === key) last[1].push(e);
    else days.push([key, [e]]);
  }

  const save = (d: NewEvent) =>
    conn && act(async () => {
      const body = { ...d, start: d.all_day ? d.start.slice(0, 10) : d.start, end: d.end ? (d.all_day ? d.end.slice(0, 10) : d.end) : "" };
      await addEvent(conn, body);
      setDraft(null);
    }, d.calendar === "google" || d.calendar === "outlook_web" ? "The calendar's new-event page is open in your browser: check it and press Save there." : `Added “${d.title.trim()}”.`);

  return (
    <>
      <section className="card">
        <div className="card-head">
          <h2>{t("Coming up")}</h2>
          {!draft && <button onClick={() => { setNote(null); setDraft(EMPTY()); }}>{t("+ NEW EVENT")}</button>}
        </div>
        <p className="dim small">
          Everything from JARVIS's own calendar, the calendars you link below and, if you turn it on, Outlook on this PC. You can
          also just ask: “what's on tomorrow?”, “find me an hour on Friday”, “add the dentist on the 12th at 4”.
        </p>

        {draft && (
          <div className="skill-form">
            <label>
              <span>{t("Title")}</span>
              <input value={draft.title} maxLength={200} placeholder="Dentist" onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
            </label>
            <label className="switch">
              <input type="checkbox" checked={draft.all_day} onChange={(e) => setDraft({ ...draft, all_day: e.target.checked })} />
              <span>{t("All day")}</span>
            </label>
            <div className="grid2">
              <label>
                <span>{t("Starts")}</span>
                <input type={draft.all_day ? "date" : "datetime-local"} value={draft.all_day ? draft.start.slice(0, 10) : draft.start}
                  onChange={(e) => setDraft({ ...draft, start: e.target.value })} />
              </label>
              <label>
                <span>{t("Ends")}</span>
                <input type={draft.all_day ? "date" : "datetime-local"} value={draft.all_day ? (draft.end ?? "").slice(0, 10) : draft.end ?? ""}
                  onChange={(e) => setDraft({ ...draft, end: e.target.value })} />
              </label>
            </div>
            <label>
              <span>{t("Where")}</span>
              <input value={draft.location} maxLength={200} onChange={(e) => setDraft({ ...draft, location: e.target.value })} />
            </label>
            <div className="grid2">
              <label>
                <span>{t("Add to")}</span>
                <select value={draft.calendar} onChange={(e) => setDraft({ ...draft, calendar: e.target.value as NewEvent["calendar"] })}>
                  <option value="jarvis">{t("JARVIS's calendar")}</option>
                  <option value="outlook">{t("Outlook on this PC")}</option>
                  <option value="google">{t("Google Calendar (opens it)")}</option>
                  <option value="outlook_web">{t("Outlook on the web (opens it)")}</option>
                </select>
              </label>
              <label>
                <span>{t("Remind me")}</span>
                <select value={draft.remind_minutes ?? ""} disabled={draft.all_day || draft.calendar === "google" || draft.calendar === "outlook_web"}
                  onChange={(e) => setDraft({ ...draft, remind_minutes: e.target.value === "" ? null : Number(e.target.value) })}>
                  <option value="">No</option>
                  <option value="10">10 minutes before</option>
                  <option value="30">30 minutes before</option>
                  <option value="60">1 hour before</option>
                </select>
              </label>
            </div>
            <div className="field-row">
              <button disabled={busy || !draft.title.trim() || !draft.start} onClick={() => save(draft)}>{t("SAVE")}</button>
              <button className="ghost" disabled={busy} onClick={() => setDraft(null)}>{t("CANCEL")}</button>
            </div>
          </div>
        )}

        {view?.problems.map((p) => <p key={p} className="bad small">{p}</p>)}
        {view && !view.events.length && <p className="dim small">{t("Nothing in the next two weeks.")}</p>}
        {days.map(([day, list]) => (
          <div key={day} className="cal-day">
            <h3>{dayLabel(day)}</h3>
            <ul className="skills">
              {list.map((e, i) => (
                <li key={`${e.id ?? e.title}-${i}`}>
                  <div className="skill-main">
                    <strong>{e.all_day ? t("All day") : `${clock(e.start)}–${clock(e.end)}`} · {e.title}</strong>
                    <span className="dim small">{[e.location, e.calendar].filter(Boolean).join(" · ")}</span>
                  </div>
                  {e.id && (
                    <div className="skill-actions">
                      <button className="ghost" disabled={busy} aria-label={`Delete ${e.title}`}
                        onClick={() => conn && window.confirm(`Delete “${e.title}”?`) && act(() => deleteEvent(conn, e.id!), "Deleted.")}>
                        {t("DELETE")}
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {note && <p className={note.tone}>{note.text}</p>}
      </section>

      <section className="card">
        <h2>{t("Linked calendars")}</h2>
        <p className="dim small">
          Link a calendar by its private address, with no sign-in and no API key. JARVIS reads it (every 10 minutes at most) and
          adds events to it by opening its own new-event page for you to save.
        </p>
        <ol className="dim small steps">
          <li><strong>Google Calendar:</strong> on calendar.google.com, open Settings, pick the calendar on the left, scroll to
            <em> Integrate calendar</em> and copy <em>Secret address in iCal format</em>.</li>
          <li><strong>Outlook.com / Microsoft 365:</strong> in Outlook on the web, Settings → Calendar → Shared calendars →
            <em> Publish a calendar</em>, choose <em>Can view all details</em>, Publish, and copy the <em>ICS</em> link.</li>
          <li>Paste it below. The address works like a password: JARVIS keeps it encrypted and never shows it to the AI.</li>
        </ol>
        {!!view?.linked.length && (
          <ul className="skills">
            {view.linked.map((c) => (
              <li key={c.name}>
                <div className="skill-main"><strong>{c.name}</strong></div>
                <div className="skill-actions">
                  <button className="ghost" disabled={busy} onClick={() => conn && act(() => unlinkCalendar(conn, c.name), `Removed ${c.name}.`)}>
                    {t("REMOVE")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="grid2">
          <label>
            <span>{t("Name")}</span>
            <input value={link.name} maxLength={40} placeholder="Work" onChange={(e) => setLink({ ...link, name: e.target.value })} />
          </label>
          <label>
            <span>{t("Private address (iCal)")}</span>
            <input type="password" autoComplete="off" value={link.url} placeholder="https://calendar.google.com/calendar/ical/…/basic.ics"
              onChange={(e) => setLink({ ...link, url: e.target.value })} />
          </label>
        </div>
        <div className="field-row">
          <button disabled={busy || !link.name.trim() || !link.url.trim()}
            onClick={() => conn && act(async () => { await linkCalendar(conn, link.name.trim(), link.url.trim()); setLink({ name: "", url: "" }); }, `Linked ${link.name.trim()}.`)}>
            {t("LINK")}
          </button>
        </div>
        <label className="switch">
          <input type="checkbox" checked={!!view?.outlook} disabled={busy || !view}
            onChange={(e) => conn && act(() => putSettings(conn, { calendar_outlook: e.target.checked }),
              e.target.checked ? "JARVIS now reads Outlook's calendar too (Outlook opens in the background when needed)." : "Outlook is no longer read.")} />
          <span>{t("Also use classic Outlook on this PC")} (whatever account it's signed in to; the new Outlook app can't be used)</span>
        </label>
      </section>
    </>
  );
}
