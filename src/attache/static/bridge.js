// The Attaché bridge. The server injects it into every artifact as
//   <script src="/bridge.js" data-token="..." data-artifact="<slug>"></script>
// or, on a share link, as <script src="/bridge.js" data-share="<link token>"></script>,
// and it gives the page window.attache = {db: {list, get, put, delete}, on, ask}.
// Pages run sandboxed at an opaque origin, so every call is a CORS request carrying the
// page's token in headers; the server allows only the collections the page declares in its
// <meta name="attache-collections" content="a,b">.
(() => {
  "use strict";
  const { token, artifact, share } = document.currentScript.dataset;
  const enc = encodeURIComponent;
  const auth = share
    ? { "X-Attache-Share": share }
    : { "X-Attache-Token": token, "X-Attache-Artifact": artifact };
  // An EventSource can't send headers, so the event stream takes the token in its query.
  const events = share ? `share=${enc(share)}` : `artifact=${enc(artifact)}&token=${enc(token)}`;

  async function call(method, path, body) {
    const init = { method, headers: { ...auth } };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (!res.ok) throw new Error((data && data.error) || text || `${res.status} ${res.statusText}`);
    return data;
  }

  const url = (collection, id) =>
    `/api/db/${enc(collection)}` + (id == null ? "" : `/${enc(String(id))}`);
  const matches = (doc, where) => Object.entries(where).every(([k, v]) => doc[k] === v);

  const db = Object.freeze({
    // list("todos") or list("todos", {list: "Home"}) to keep docs whose fields equal those values
    list: async (collection, where) => {
      const docs = await call("GET", url(collection));
      return where ? docs.filter((doc) => matches(doc, where)) : docs;
    },
    get: (collection, id) => call("GET", url(collection, id)),
    // put replaces the whole doc; id null (or undefined) creates one and returns it with its id
    put: (collection, id, data) =>
      id == null ? call("POST", url(collection), data) : call("PUT", url(collection, id), data),
    delete: (collection, id) => call("DELETE", url(collection, id)),
  });

  const listeners = new Map(); // collection -> Set of callbacks
  let source = null;

  // on("todos", ({collection, id, doc}) => ...) fires on every change; doc is null when deleted.
  // Returns a function that stops listening.
  function on(collection, callback) {
    if (!listeners.has(collection)) listeners.set(collection, new Set());
    listeners.get(collection).add(callback);
    if (!source) {
      source = new EventSource(`/api/events?${events}`);
      source.addEventListener("doc", (event) => {
        const change = JSON.parse(event.data);
        for (const fn of listeners.get(change.collection) || []) {
          try {
            fn(change);
          } catch (err) {
            console.error("attache.on callback failed", err);
          }
        }
      });
    }
    return () => listeners.get(collection).delete(callback);
  }

  // ask("Which of these are overdue?") resolves to the assistant's reply, as text. The turn
  // sees only this page's collections. Pages open in the Attaché window only: never on a
  // share link.
  async function ask(prompt) {
    if (share) throw new Error("not available on shared links");
    const { reply } = await call("POST", "/api/ask", { prompt: String(prompt) });
    return reply;
  }

  window.attache = Object.freeze({ db, on, ask });
})();
