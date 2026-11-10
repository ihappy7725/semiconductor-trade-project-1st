"use strict";
// (junhee) 2026-09-27 등록 샘플이 아닌 기업 파일 → 서버 분석 엔진(sanghyeob suitability-reference-v1) → 대시보드 문서(handoff-v1).
// 스타일 없음. 서버: junhee/server/analysis.py · 변환: junhee/server/engine_adapter.py
(() => {
  const auth = window.JunheeAuth;
  if (!auth) return;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const message = (e, fallback) => (e && e.data && e.data.message) || fallback;
  // (2026-09-27 배포 QA) 서버 오류 코드를 사용자 문구로 (코드를 그대로 보여 주지 않는다)
  const CODE_TEXT = {
    auth_unavailable: "로그인 확인 서버에 잠시 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    analysis_storage_unavailable: "분석 저장소를 잠시 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    assessment_not_found: "분석 기록을 찾지 못했습니다(서버 재시작 등). 다시 분석해 주세요.",
    not_found: "저장된 분석 결과를 찾지 못했습니다. 다시 분석해 주세요.",
    request_failed: "서버와 통신하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    analysis_running: "이 계정의 다른 분석이 진행 중입니다. 완료 후 다시 시도해 주세요. 중단된 작업은 서버 실행 제한이 지나면 자동으로 해제됩니다.",
    analysis_busy: "다른 분석 작업을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
  };
  const readable = (e, fallback) => message(e, CODE_TEXT[e && e.message] || fallback);
  async function request(url, opts = {}, timeout = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      return await auth.api(url, { ...opts, signal: controller.signal });
    } catch (e) {
      if (controller.signal.aborted || e.status === 504) {
        throw new Error("분석 서버의 응답 대기 시간이 초과되었습니다. 서버에서 계속 처리 중일 수 있습니다. 잠시 후 같은 파일과 조건으로 다시 시도해 주세요.");
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  async function submit(inputs, progress) {
    const url = "/api/analysis/assessments";
    try {
      return await request(url, { method: "POST", json: inputs }, 310000);
    } catch (e) {
      const active = e.data && e.data.active;
      if (e.status !== 409 || e.message !== "analysis_running" || !active) throw e;
      const prompt = {
        title: "이전 분석이 남아 있습니다",
        message: `이전 작업 상태: ${active.progress || "분석 중"}. 이전 분석을 중단하고 지금 선택한 파일로 새 분석을 시작할까요?`,
        ok: "이전 분석 중단 후 시작", cancel: "돌아가기",
      };
      const replace = window.JunheeDashboard
        ? await window.JunheeDashboard.confirm(prompt)
        : window.confirm(prompt.message);
      if (!replace) throw new Error("새 분석을 시작하지 않았습니다. 이전 분석과 같은 파일·조건으로 실행하면 진행 상황을 확인할 수 있습니다.");
      progress("이전 분석을 중단하고 새 분석을 시작합니다…");
      await request(`${url}/${encodeURIComponent(active.assessment_id)}/cancel`, { method: "POST" });
      return await request(url, { method: "POST", json: inputs }, 310000);
    }
  }

  async function upload(raw, name) {
    const fd = new FormData();
    fd.append("file", raw, name);
    const res = await fetch("/api/analysis/sources", { method: "POST", body: fd, credentials: "same-origin", headers: { "X-CSRF-Token": auth.csrf, Accept: "application/json" } });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (res.status === 401) { location.href = "/app?auth=expired"; throw new Error("authentication_required"); }
    if (!res.ok) throw new Error((data && data.message) || "파일을 올리지 못했습니다.");
    return data;
  }

  // file: 바탕화면 파일(raw = 브라우저 File, source_id = 서버에 올린 원본 id) · cond: {company, hs, country(ISO2)}
  async function run(file, cond, progress = () => {}) {
    let companyId = file.source_id ? "upload:" + file.source_id : file.sample && file.company_id ? file.company_id : null; // 샘플은 서버의 샘플 파일(static/samples)로
    if (file.raw) {
      progress("파일을 분석 서버로 올리는 중입니다…");
      const up = await upload(file.raw, file.name);
      companyId = up.company_id;
      file.source_id = companyId.slice(7);
    }
    if (!companyId) throw new Error("원본 파일이 서버에 없습니다. 파일을 다시 올려 주세요.");
    // (2026-09-27) 결측 확인: 빈 칸·빠진 시트가 있으면 어느 셀인지와 점수 영향을 보여 주고 진행 여부를 묻는다(값을 채우지 않음)
    progress("파일의 빈 칸을 확인합니다…");
    const chk = await request("/api/analysis/inspect?company_id=" + encodeURIComponent(companyId)).catch(() => null);
    if (chk && (chk.fields.length || chk.missing_sheets.length || chk.issues.length) && window.JunheeDashboard && !(await confirmMissing(chk))) {
      const e = new Error("결측 확인 후 분석을 취소했습니다. 파일을 보완해 다시 올리거나 '그래도 진행'을 선택하세요.");
      e.cancelled = true;
      throw e;
    }
    const started = Date.now();
    const deadline = started + 15 * 60 * 1000;
    const waiting = () => progress(`5개국 무역통계·환율·규제 자료를 조회하고 분석합니다… (${Math.floor((Date.now() - started) / 1000)}초 경과)`);
    waiting();
    // Vercel executes the analysis inside this POST (up to 300 seconds).
    // Keep feedback and a request deadline active before polling starts too.
    const feedback = setInterval(waiting, 1000);
    let st;
    try {
      st = await submit({ company_id: companyId, company: cond.company, hs: cond.hs, country: cond.country }, progress);
    } catch (e) {
      if (e.status === 404) { delete file.source_id; throw new Error("서버에 원본 파일이 없습니다(서버 재시작 등). 파일을 다시 올려 주세요."); }
      const fallback = e.status === 502 || e.status === 503
        ? "분석 서버에 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        : e.status === 403 ? "로그인 보안 정보가 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요."
        : e.message !== "request_failed" ? e.message : "서버와 통신하지 못했습니다. 잠시 후 다시 시도해 주세요.";
      throw new Error(readable(e, fallback || "분석을 시작하지 못했습니다."));
    } finally {
      clearInterval(feedback);
    }
    let fails = 0; // (2026-09-27 배포 QA) 일시 오류(5xx·429·네트워크)는 간격을 늘려 다시 묻는다. 404 등은 바로 중단
    while (st.status === "QUEUED" || st.status === "RUNNING") {
      progress(st.progress || "분석 중입니다…");
      if (Date.now() >= deadline) throw new Error("분석이 너무 오래 걸립니다. 서버에서는 계속 진행될 수 있으니 잠시 후 같은 조건으로 다시 분석하면 이어서 확인합니다.");
      await sleep(fails ? Math.min(1500 * 2 ** fails, 12000) : 1500);
      let stageMessage = st.progress || "보고서 근거를 수집합니다…";
      let stageActive = true, polling = false;
      const stageFeedback = setInterval(() => progress(`${stageMessage} (${Math.floor((Date.now() - started) / 1000)}초 경과)`), 1000);
      const liveStatus = st.stepwise ? setInterval(async () => {
        if (polling) return;
        polling = true;
        try {
          const live = await request(`/api/analysis/assessments/${encodeURIComponent(st.assessment_id)}`);
          if (stageActive && live.progress) stageMessage = live.progress;
        } catch { /* The advance request owns completion and retry handling. */ }
        finally { polling = false; }
      }, 8000) : null;
      try {
        const url = `/api/analysis/assessments/${encodeURIComponent(st.assessment_id)}`;
        st = st.stepwise
          ? await request(url + "/advance", { method: "POST" }, Math.max(1, Math.min(150000, deadline - Date.now())))
          : await request(url, {}, Math.max(1, Math.min(30000, deadline - Date.now())));
        fails = 0;
      } catch (e) {
        const transient = !e.status || e.status >= 500 || e.status === 429;
        if (!transient || ++fails > 5) throw new Error(readable(e, "분석 진행 상태를 확인하지 못했습니다."));
      } finally {
        stageActive = false;
        clearInterval(stageFeedback);
        if (liveStatus) clearInterval(liveStatus);
      }
    }
    if (st.status !== "COMPLETED") throw new Error(st.progress || "분석을 완료하지 못했습니다.");
    progress("대시보드에 결과를 여는 중입니다…");
    try {
      return await load(st.assessment_id);
    } catch (e) {
      throw new Error(readable(e, "분석 결과를 불러오지 못했습니다."));
    }
  }
  function confirmMissing(chk) {
    const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    const scored = chk.fields.filter((f) => f.scored).length;
    const rows = chk.fields.map((f) => `<tr><td>${esc(f.label)}</td><td>${esc(f.cell)}</td><td>${f.scored ? "<b>점수 영향</b>" : "참고"}</td><td>${esc(f.impact)}</td></tr>`).join("")
      + chk.missing_sheets.map((m) => `<tr><td>${esc(m)} 시트</td><td>—</td><td><b>빠짐</b></td><td>이 시트로 계산하는 항목은 자료 부족</td></tr>`).join("")
      + chk.issues.slice(0, 20).map((m) => `<tr><td colspan="2">검증</td><td>오류</td><td>${esc(m)}</td></tr>`).join("");
    const html = `<div class="jd-table-wrap"><table class="jd-data-table"><thead><tr><th>항목</th><th>셀</th><th>구분</th><th>영향</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    const n = chk.fields.length + chk.missing_sheets.length + chk.issues.length;
    return JunheeDashboard.confirm({ eyebrow: "DATA CHECK", title: "결측치가 있는 파일입니다", wide: true, html,
      message: `${chk.file_name} 에서 빈 칸·누락 ${n}건을 찾았습니다${scored ? ` (점수에 영향 ${scored}건)` : ""}. 빈 칸은 값을 채우지 않고 '자료 부족'으로 두며, 해당 점수 항목은 정책 기준 50점으로 계산합니다. 그래도 진행하시겠습니까?`,
      ok: "그래도 진행", cancel: "취소" });
  }
  const load = (id) => auth.api(`/api/analysis/assessments/${encodeURIComponent(id)}/handoff`);
  const forget = (id) => auth.api(`/api/analysis/assessments/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => null);

  // (2026-09-27) 영구 삭제한 업로드 원본. 성공(이미 없음 404 포함)이면 true, 진행 중(409)·네트워크 오류면 false → 다음 저장 때 다시 시도
  const forgetSource = (id) => auth.api(`/api/analysis/sources/${encodeURIComponent(id)}`, { method: "DELETE" }).then(() => true, (e) => e && e.status === 404);
  window.JunheeAnalysis = { run, load, forget, forgetSource };
})();
