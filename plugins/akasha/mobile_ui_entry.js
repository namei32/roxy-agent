function mountAkasha(host, context) {
  host.innerHTML = `<section class="akasha-mobile-root">
    <nav class="akmg-tabs" aria-label="Akasha 页面">
      <button type="button" data-akasha-tab="graph" aria-pressed="true">记忆图</button>
      <button type="button" data-akasha-tab="inspector" aria-pressed="false">Inspector</button>
    </nav><div data-akasha-content></div></section>`;
  const content = host.querySelector("[data-akasha-content]");
  let cleanup;
  const select = (name) => {
    cleanup?.();
    host.querySelectorAll("[data-akasha-tab]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.akashaTab === name));
    });
    cleanup = name === "graph" ? mountMemoryGraph(content, context) : mountInspector(content, context);
  };
  host.querySelectorAll("[data-akasha-tab]").forEach((button) => {
    button.addEventListener("click", () => select(button.dataset.akashaTab));
  });
  select("graph");
  return () => { cleanup?.(); host.replaceChildren(); };
}

export default {
  slots: { "turn.before_reasoning": { mount: mountRecall } },
  dashboard: { mount: mountAkasha },
};
