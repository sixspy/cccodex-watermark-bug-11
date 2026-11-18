(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = {};
  let templates = [];
  let batches = [];
  let reports = [];
  let board = null;
  let adjudications = [];
  let reconcileItems = [];
  let selectedId = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || "HTTP " + r.status);
      err.reasonCode = j.reason_code || "";
      err.details = j.details || {};
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    board = await api("/api/readiness");
    adjudications = (await api("/api/adjudications")).items || [];
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function policyChip() {
    const p = (meta && meta.policy) || { label: "留存候审", text: "" };
    return `<p class="policy"><span class="chip policy-chip">命运：${esc(
      p.label
    )}</span> <span class="muted">${esc(p.text)}</span></p>`;
  }

  function fateChip(fate) {
    if (!fate) return "";
    return `<span class="chip fate-${esc(fate.tone || "muted")}">${esc(
      fate.label
    )}</span>`;
  }

  function reasonChip(code, text) {
    if (!code) return "";
    return `<span class="reason-chip" title="${esc(text || "")}">拒因：${esc(
      code
    )}</span>`;
  }

  function assessmentFor(id) {
    if (!board) return null;
    return (board.assessments || []).find((a) => a.batch_id === id) || null;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      ${policyChip()}
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${esc(t.version_no)}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  // 靶表附页：每张靶表一张附页，色块直接取裁定结果（不是另写一套判断）。
  function renderBatches() {
    const tables = (board && board.target_tables) || [];
    const cards = tables
      .map((t) => {
        const tone =
          t.blocked_count > 0 ? "warn" : t.writable_count > 0 ? "ok" : "muted";
        return `
        <section class="cardish table-page" data-tone="${tone}">
          <div class="table-head">
            <h3>${esc(t.table_name)}</h3>
            <span class="swatch swatch-${tone}" aria-hidden="true"></span>
          </div>
          <p class="muted small">
            批次 ${t.batch_count} · 齐备 ${t.ready_count} · 受阻滞留 ${t.blocked_count} ·
            可写印 ${t.writable_count} · 候审 ${t.quarantined_count}
          </p>
          <table>
            <thead><tr><th>编号</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th>命运</th><th></th></tr></thead>
            <tbody>
              ${t.batches
                .map(
                  (a) => `<tr class="row-${a.readiness.ready ? "ready" : "blocked"}">
                <td>${a.batch_id}</td>
                <td>${esc(a.run_code)}</td>
                <td>${esc(
                  (batches.find((b) => b.id === a.batch_id) || {}).biz_date || "-"
                )}</td>
                <td><span class="chip">${esc(a.status)}</span></td>
                <td>${esc(
                  (batches.find((b) => b.id === a.batch_id) || {}).watermark_value ||
                    "-"
                )}</td>
                <td>${fateChip(a.fate)}</td>
                <td>${
                  a.writable
                    ? `<button type="button" class="ghost" data-pick="${a.batch_id}">盖印</button>`
                    : `<button type="button" class="ghost" data-inspect="${a.batch_id}">看拒因</button>`
                }</td>
              </tr>`
                )
                .join("")}
            </tbody>
          </table>
          ${
            t.blocked_count > 0
              ? `<p class="blocker-note">${esc(
                  t.batches
                    .filter((a) => !a.readiness.ready)
                    .map((a) => `${a.run_code}：${a.reason_text || ""}`)
                    .join("；")
                )}</p>`
              : ""
          }
        </section>`;
      })
      .join("");

    return `
      <h2>靶表附页</h2>
      ${policyChip()}
      <p class="muted small">主链：选批盖印 → 用水印追索下游 → 缺口对账。色块与命运取自同一裁定。</p>
      ${cards || `<p class="muted">暂无靶表</p>`}
      ${renderStampCard()}
      <div id="inspect-box"></div>`;
  }

  // 写印卡片：亮拒因芯片，命运标记与齐备席同源。
  function renderStampCard() {
    if (selectedId == null) return "";
    const a = assessmentFor(selectedId);
    if (!a) return "";
    const b = batches.find((x) => x.id === selectedId) || {};
    const chips = [];
    if (!a.readiness.ready) {
      a.readiness.blockers.forEach((bl) => {
        chips.push(reasonChip(bl.reason_code, bl.reason_text));
      });
    }
    if (!a.template.ok) chips.push(reasonChip("NO_CURRENT_TEMPLATE", "无现行模板"));
    if (a.reason_code) chips.push(reasonChip(a.reason_code, a.reason_text));

    return `
      <div class="cardish stamp-card" data-tone="${esc(a.fate.tone)}">
        <div class="table-head">
          <h3>写印卡片</h3>
          ${fateChip(a.fate)}
        </div>
        <p>${esc(b.run_code || a.run_code)} · ${esc(a.table_name)}</p>
        <div class="chip-row">
          ${a.readiness.ready
            ? `<span class="chip fate-ok">源表齐备 ${a.readiness.ready_source_count}/${a.readiness.source_count}</span>`
            : `<span class="chip fate-warn">源表未齐备 ${a.readiness.ready_source_count}/${a.readiness.source_count}</span>`}
          ${chips.join("")}
        </div>
        ${
          a.writable
            ? `<label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
               <button type="button" id="btn-stamp">点写印</button>`
            : `<p class="deny-line">拒因：${esc(
                a.reason_text || "不可写印"
              )}</p><button type="button" class="ghost" disabled>点写印（已拒）</button>`
        }
        <p class="muted small">写印时后端一次裁定；拒因码可在此核对。</p>
      </div>`;
  }

  // 齐备席：逐表命运标记 + 遗留在途空边印的收口动作。
  function renderReadiness() {
    const counts = (board && board.counts) || {};
    const rows = ((board && board.assessments) || [])
      .map(
        (a) => `<tr>
        <td>${esc(a.run_code)}</td>
        <td>${esc(a.table_name)}</td>
        <td><span class="chip">${esc(a.status)}</span></td>
        <td>${
          a.readiness.ready
            ? `<span class="chip fate-ok">齐备 ${a.readiness.ready_source_count}/${a.readiness.source_count}</span>`
            : `<span class="reason-chip">未齐 ${a.readiness.ready_source_count}/${a.readiness.source_count}</span>`
        }</td>
        <td>${fateChip(a.fate)}</td>
        <td>${a.reason_code ? reasonChip(a.reason_code, a.reason_text) : ""}</td>
      </tr>`
      )
      .join("");

    const edges = ((board && board.edges) || [])
      .map(
        (e) => `<tr class="${e.empty_edge ? "row-blocked" : "row-ready"}">
        <td>${esc(e.upstream_run_code)}</td>
        <td><span class="chip">${esc(e.upstream_status)}</span></td>
        <td>${esc(e.downstream_run_code)}</td>
        <td><span class="chip">${esc(e.downstream_status)}</span></td>
        <td>${
          e.empty_edge
            ? `<span class="reason-chip">空边</span>`
            : `<span class="chip fate-ok">有效</span>`
        }</td>
      </tr>`
      )
      .join("");

    const hasLegacy =
      ((board && board.assessments) || []).some(
        (a) => a.status === "已盖印" && !a.readiness.ready
      );

    return `
      <h2>齐备席</h2>
      ${policyChip()}
      <div class="stat-row">
        <span class="stat"><b>${counts.batches || 0}</b> 批</span>
        <span class="stat"><b>${counts.writable || 0}</b> 可写印</span>
        <span class="stat danger"><b>${counts.blocked || 0}</b> 未齐</span>
        <span class="stat review"><b>${counts.quarantined || 0}</b> 候审</span>
        <span class="stat"><b>${counts.empty_edges || 0}</b> 空边</span>
      </div>
      ${
        hasLegacy
          ? `<div class="row"><button type="button" id="btn-sweep">将遗留空边印收进候审只读</button></div>`
          : ""
      }
      <h3>批次命运</h3>
      <table>
        <thead><tr><th>运行码</th><th>表</th><th>状态</th><th>源齐备</th><th>命运</th><th>拒因</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <h3>血缘边目录</h3>
      <p class="muted small">空边 = 上游未有效盖印；下游追索不再把它当作有效来源。</p>
      <table>
        <thead><tr><th>上游</th><th>上游状态</th><th>下游</th><th>下游状态</th><th>边</th></tr></thead>
        <tbody>${edges}</tbody>
      </table>`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    const warn = traceResult && traceResult.used_empty_edges;
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        ${
          warn
            ? `<p class="deny-line">该水印只有空边来源，已拒绝作为有效上游；下游结果为空。</p>`
            : ""
        }
        <h3>源批次</h3>
        <ul>${src
          .map(
            (s) =>
              `<li>${esc(s.run_code)} · ${esc(s.table_name)} · <span class="chip">${esc(
                s.status
              )}</span> ${
                s.valid
                  ? `<span class="chip fate-ok">有效</span>`
                  : `<span class="reason-chip">空边</span>`
              }</li>`
          )
          .join("")}</ul>
        <h3>下游</h3>
        <ul>${
          down.length
            ? down
                .map(
                  (d) =>
                    `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(
                      d.watermark_value || "未盖印"
                    )}</li>`
                )
                .join("")
            : `<li class="muted">无有效下游</li>`
        }</ul>
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    const recon = reconcileItems
      .map(
        (i) => `<tr>
        <td>${esc(i.run_code)}</td>
        <td>${esc(i.table_name)}</td>
        <td><span class="chip">${esc(i.status)}</span></td>
        <td>${
          i.bucket === "有效盖印"
            ? `<span class="chip fate-ok">${esc(i.bucket)}</span>`
            : i.bucket === "候审只读"
            ? `<span class="reason-chip">${esc(i.bucket)}</span>`
            : `<span class="chip fate-warn">${esc(i.bucket)}</span>`
        }</td>
      </tr>`
      )
      .join("");
    return `
      <h2>缺口对账</h2>
      <p class="muted small">口径：有效盖印才算数；空边印计入候审，不被静默抹掉。</p>
      <button type="button" id="btn-gap">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>候审</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${esc(r.id)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${esc(r.gap_count)}</td>
            <td>${esc(r.review_count)}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        recon
          ? `<h3>逐批复核</h3><table>
              <thead><tr><th>运行码</th><th>表</th><th>状态</th><th>归类</th></tr></thead>
              <tbody>${recon}</tbody>
            </table>`
          : ""
      }`;
  }

  function renderAudit() {
    return `
      <h2>写印裁定流水</h2>
      <p class="muted small">每次点写印都留痕：允许与拒绝都可核，供编排任务对账。</p>
      <table>
        <thead><tr><th>编号</th><th>运行码</th><th>裁定</th><th>拒因码</th><th>白话</th><th>值班人</th><th>时间</th></tr></thead>
        <tbody>
          ${adjudications
            .map(
              (a) => `<tr class="${a.verdict === "allowed" ? "row-ready" : "row-blocked"}">
            <td>${esc(a.id)}</td>
            <td>${esc(a.run_code || "-")}</td>
            <td>${
              a.verdict === "allowed"
                ? `<span class="chip fate-ok">允许</span>`
                : `<span class="reason-chip">拒绝</span>`
            }</td>
            <td>${esc(a.reason_code)}</td>
            <td>${esc(a.reason_text)}</td>
            <td>${esc(a.operator)}</td>
            <td>${esc(a.created_at)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "readiness") view.innerHTML = renderReadiness();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else if (tab === "audit") view.innerHTML = renderAudit();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        selectedId = Number(btn.dataset.pick);
        const a = assessmentFor(selectedId);
        const b = batches.find((x) => x.id === selectedId);
        stampValue =
          a && a.run_code ? "WM-" + a.run_code.toUpperCase() : b ? "WM-" + b.run_code.toUpperCase() : "";
        render();
      };
    });
    view.querySelectorAll("[data-inspect]").forEach((btn) => {
      btn.onclick = () => {
        const a = assessmentFor(Number(btn.dataset.inspect));
        const box = document.getElementById("inspect-box");
        if (!a || !box) return;
        const items = (a.readiness.blockers || [])
          .map((b) => `<li>${esc(b.reason_text)} <code>${esc(b.reason_code)}</code></li>`)
          .join("");
        box.innerHTML = `<div class="cardish inspect-card">
          <h3>拒因核对 · ${esc(a.run_code)}</h3>
          <p>命运：${fateChip(a.fate)}</p>
          <ul>${items || `<li class="muted">无阻塞</li>`}</ul>
          <p class="muted small">拒因码与点写印时后端返回的 reason_code 同源。</p>
        </div>`;
      };
    });

    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
    }
    const stampBtn = document.getElementById("btn-stamp");
    if (stampBtn) {
      stampBtn.onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selectedId + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selectedId = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg((e.reasonCode ? "[" + e.reasonCode + "] " : "") + e.message);
          await refresh().catch(() => {});
          render();
        }
      };
    }

    const sweepBtn = document.getElementById("btn-sweep");
    if (sweepBtn) {
      sweepBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/readiness/sweep", { method: "POST" });
          await refresh();
          setMsg("已收进候审只读：" + (r.moved || []).map((m) => m.run_code).join("、"));
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }

    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg(
            traceResult.used_empty_edges
              ? "追索完成：仅命中空边印，未展开下游"
              : "追索完成"
          );
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }

    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          reconcileItems = (await api("/api/reconcile")).items || [];
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
