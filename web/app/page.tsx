"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type Section = {
  class_number: string; section: string; component: string; status: string;
  available_seats: number; capacity: number; enrolled: number;
  meetings: string[]; restrictions: string;
};
type Bundle = { key: string; class_numbers: string[]; open: boolean; inferred: boolean; schedule_unknown: boolean };
type Watch = {
  id: number; term: string; subject: string; catalog_number: string; class_number: string;
  state: string; detail: string; checked_at: string | null; observed_at: string | null; fresh: boolean;
  snapshot: { sections: Section[]; bundles: Bundle[]; issues: string[]; pairing_note: string } | null;
};

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options, headers: { "Content-Type": "application/json", ...options?.headers }, cache: "no-store",
  });
  if (response.status === 204) return undefined as T;
  const body = await response.json();
  if (!response.ok) {
    const message = typeof body.detail === "string" ? body.detail : "Please check your input and try again.";
    throw new Error(response.status === 401 ? "session_expired" : message);
  }
  return body as T;
}
const date = (value: string | null) => value ? new Date(value).toLocaleString() : "Not checked yet";
const stateLabel: Record<string, string> = {
  pending: "Queued", term_unpublished: "Term not published", course_unpublished: "Course not published",
  error: "Check interrupted", observed: "Monitoring",
};

export default function Home() {
  const [email, setEmail] = useState("");
  const [account, setAccount] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [watches, setWatches] = useState<Watch[]>([]);
  const [course, setCourse] = useState("");
  const [classNumber, setClassNumber] = useState("");

  const refresh = useCallback(async () => {
    try { setWatches(await api<Watch[]>("/watches")); }
    catch (err) {
      const text = err instanceof Error ? err.message : "Unable to load watches";
      if (text === "session_expired") { setAccount(null); setWatches([]); setCodeSent(false); }
      else setError(text);
    }
  }, []);
  useEffect(() => {
    let active = true;
    api<{ email: string }>("/me").then(async user => {
      if (active) { setAccount(user.email); await refresh(); }
    }).catch(err => {
      if (active && err.message !== "session_expired") setError("Unable to reach Course Watch. Try again shortly.");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refresh]);
  useEffect(() => {
    if (!account) return;
    const timer = setInterval(refresh, 30_000);
    return () => clearInterval(timer);
  }, [account, refresh]);

  async function action(work: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await work(); }
    catch (err) {
      const text = err instanceof Error ? err.message : "Something went wrong. Please try again.";
      if (text === "session_expired") { setAccount(null); setWatches([]); setCodeSent(false); setError("Please sign in again."); }
      else setError(text);
    } finally { setBusy(false); }
  }
  function login(event: FormEvent) {
    event.preventDefault();
    void action(async () => {
      if (!codeSent) {
        await api("/auth/request-code", { method: "POST", body: JSON.stringify({ email }) });
        setCodeSent(true); setMessage("Check your email for a six-digit code. It expires in 10 minutes.");
      } else {
        const user = await api<{ email: string }>("/auth/verify", { method: "POST", body: JSON.stringify({ email, code }) });
        setAccount(user.email); setCode(""); setCodeSent(false); await refresh();
      }
    });
  }
  function addWatch(event: FormEvent) {
    event.preventDefault();
    void action(async () => {
      await api("/watches", { method: "POST", body: JSON.stringify({
        term: "2027 Spring", subject: "COMPSCI", catalog_number: course.trim().toUpperCase(), class_number: classNumber.trim(),
      }) });
      setCourse(""); setClassNumber(""); setMessage("Watch added. The first successful check establishes a baseline; alerts follow new openings.");
      await refresh();
    });
  }
  return <div className="shell">
    <header className="topbar"><a className="brand" href="/" aria-label="Course Watch home"><span className="mark">CW</span> Course Watch</a>
      <span className="campus">UMASS AMHERST</span>
      {account && <button disabled={busy} className="text-button" onClick={() => void action(async () => {
        await api("/auth/logout", { method: "POST" }); setAccount(null); setWatches([]); setCodeSent(false);
      })}>Sign out</button>}
    </header>
    <main>
      <div className="intro"><p className="eyebrow"><span className="dot" /> SPRING 2027 · COMPUTER SCIENCE</p>
        <h1>Don&apos;t let the fear of missing out on your preferred classes haunt you any longer. <span>Say goodbye to scheduling uncertainties and hello to a smoother educational experience!</span></h1>
        <p className="lede">Keep an eye on the classes you need. Get an email when a lecture and its related sections have room.</p>
      </div>
      {error && <div className="notice error" role="alert">{error}</div>}
      {message && <div className="notice" role="status">{message}</div>}
      {loading ? <p role="status">Loading your watchlist…</p> : !account ?
        <section className="login card"><div><p className="eyebrow">YOUR CAMPUS, YOUR INBOX</p><h2>Start your watchlist</h2>
          <p>Sign in with your UMass email. A one-time code verifies your address; no password needed.</p>
          <ul className="features"><li>Email alerts for new openings</li><li>One watchlist, ready for registration</li></ul>
        </div><form onSubmit={login}><label htmlFor="email">UMass email</label>
          <input id="email" type="email" autoComplete="email" placeholder="you@umass.edu" required value={email} readOnly={codeSent} onChange={e => setEmail(e.target.value)} />
          {codeSent && <><label htmlFor="code">Verification code</label><input id="code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} placeholder="000000" required value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ""))} /></>}
          <button className="primary" disabled={busy} type="submit">{busy ? "Please wait…" : codeSent ? "Verify & sign in" : "Email me a sign-in code"}<span aria-hidden>↗</span></button>
          {codeSent && <button type="button" disabled={busy} className="text-button" onClick={() => { setCodeSent(false); setCode(""); setMessage(""); }}>Change email or request a new code</button>}
          <small>Only @umass.edu addresses are supported.</small>
        </form></section> : <>
          <section className="watch-heading"><div><p className="eyebrow">SIGNED IN AS {account}</p><h2>Your watchlist <span className="count">{watches.length}</span></h2></div><button className="secondary" disabled={busy} onClick={() => void action(refresh)}>Refresh status</button></section>
          <form className="add-form card" onSubmit={addWatch}>
            <div><label htmlFor="term">Term</label><input id="term" value="Spring 2027" readOnly /></div>
            <div><label htmlFor="subject">Subject</label><input id="subject" value="COMPSCI" readOnly /></div>
            <div><label htmlFor="course">Course number</label><input id="course" placeholder="220" pattern="[1-9][0-9]{2}[A-Za-z]?" maxLength={4} required value={course} onChange={e => setCourse(e.target.value)} /></div>
            <div><label htmlFor="section">Class number <span className="optional">optional</span></label><input id="section" inputMode="numeric" placeholder="Any section" pattern="[0-9]{5}" maxLength={5} value={classNumber} onChange={e => setClassNumber(e.target.value.replace(/\D/g, ""))} /></div>
            <button className="primary" disabled={busy || watches.length >= 20} type="submit">Add watch <span aria-hidden>+</span></button>
          </form>
          <p className="helper">Leave class number blank to watch any section. A five-digit class number targets a lecture, lab, or discussion and its required companions.</p>
          {!watches.length && <div className="empty"><span aria-hidden>◎</span><h3>Your next semester starts here.</h3><p>Add a Computer Science course to start watching. You can queue it before Spring classes are published.</p></div>}
          <div className="watch-list">{watches.map(watch => {
            const bundles = watch.snapshot?.bundles.filter(b => !watch.class_number || b.class_numbers.includes(watch.class_number)) || [];
            const opened = bundles.filter(b => b.open);
            return <article className="watch card" key={watch.id}>
              <div className="watch-top"><div><p className="eyebrow">SPRING 2027 · {watch.class_number ? `CLASS ${watch.class_number}` : "ANY SECTION"}</p><h3>COMPSCI <span>{watch.catalog_number}</span></h3></div>
                <span className={`badge ${watch.fresh && opened.length ? "open" : ""}`}>{watch.fresh && opened.length ? `${opened.length} open combination${opened.length === 1 ? "" : "s"}` : stateLabel[watch.state] || "Pending"}</span>
              </div>
              <p>{watch.detail}</p>
              {watch.snapshot && <>
                {!watch.fresh && <p className="stale">Saved observation · availability may have changed.</p>}
                <details><summary>Sections & related combinations ({bundles.length})</summary>
                  <div className="table-scroll"><table><thead><tr><th>Section / class</th><th>Status</th><th>Seats</th><th>Meetings</th></tr></thead>
                    <tbody>{watch.snapshot.sections.map(s => <tr key={s.class_number}><td><strong>{s.section}-{s.component}</strong><br /><small>{s.class_number}</small></td><td>{s.status}</td><td>{s.available_seats} / {s.capacity}</td><td>{s.meetings.join("; ") || "TBA"}</td></tr>)}</tbody>
                  </table></div>
                  {opened.length > 0 && <div className="combos"><h4>Open at last observation</h4>{opened.map(b => <p key={b.key}>{b.class_numbers.join(" + ")}{b.inferred && <small> · pairing inferred</small>}{b.schedule_unknown && <small> · meeting times incomplete</small>}</p>)}</div>}
                  {!bundles.length && <p>No unambiguous combinations found{watch.class_number ? " for this class number" : ""}. Check the class number and related sections in SPIRE.</p>}
                  {watch.snapshot.issues.map(issue => <p key={issue} className="helper">{issue}</p>)}
                  {watch.snapshot.sections.filter(s => s.restrictions).map(s => <p className="restrictions" key={s.class_number}><strong>{s.section}-{s.component}:</strong> {s.restrictions}</p>)}
                </details>
                <p className="helper">{watch.snapshot.pairing_note}</p>
              </>}
              <div className="watch-bottom"><small>Last check: {date(watch.checked_at)}{watch.observed_at && <><br />Last observation: {date(watch.observed_at)}</>}</small>
                <button className="text-button" disabled={busy} aria-label={`Remove COMPSCI ${watch.catalog_number} watch`} onClick={() => void action(async () => { await api(`/watches/${watch.id}`, { method: "DELETE" }); await refresh(); })}>Remove watch</button></div>
            </article>;
          })}</div>
        </>}
      <aside className="footnote"><strong>A heads-up, then you take it from there.</strong><p>Confirm compatibility, prerequisites, and reserved seats in <a href="https://www.spire.umass.edu/" target="_blank" rel="noreferrer">SPIRE ↗</a>. We watch availability; you handle enrollment.</p></aside>
    </main>
    <footer><span>Course Watch</span><span>An independent project. Not affiliated with UMass Amherst.</span></footer>
  </div>;
}
