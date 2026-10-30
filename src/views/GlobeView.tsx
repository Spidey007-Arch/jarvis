import { useEffect, useState } from "react";
import { startGlobe } from "../api";
import type { Connection } from "../shell";

/** God's Eye View, the live 3D globe, shown inside JARVIS. Started on first open; kept loaded while hidden. */
export default function GlobeView({ conn, open, target, onClose }: { conn: Connection | null; open: boolean; target: string | null; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [note, setNote] = useState("Starting the globe… the first start can take a minute or two.");

  useEffect(() => {
    if (!open || url || !conn) return;
    let gone = false;
    startGlobe(conn)
      .then((r) => {
        if (gone) return;
        if (r.running && r.url) setUrl(`${r.url}/?embed=1`); // the embed page is the one it lets JARVIS show
        else setNote(r.error ?? "The globe did not start.");
      })
      .catch((e) => !gone && setNote(String(e)));
    return () => {
      gone = true;
    };
  }, [open, url, conn]);

  // JARVIS asked to show something: point the globe at it.
  useEffect(() => {
    if (target && (!url || target.split("#")[0] === url.split("#")[0])) setUrl(target);
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className={`globe-view ${open ? "open" : ""}`} aria-hidden={!open} aria-label="God's Eye View globe">
      <header className="globe-head">
        <span>GOD'S EYE VIEW</span>
        <em>by Bilawal Sidhu · MIT</em>
        <button className="close" onClick={onClose} aria-label="Close the globe">✕</button>
      </header>
      {url ? (
        <iframe src={url} title="God's Eye View" allow="geolocation; microphone; fullscreen; clipboard-write" />
      ) : (
        <div className="view-msg">{note}</div>
      )}
    </section>
  );
}
