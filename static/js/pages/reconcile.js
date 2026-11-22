import { api } from '../api.js';

export async function renderReconcile(el) {
  const data = await api('/api/reports');
  const dash = await api('/api/dashboard');
  const cf = dash.cabin_full || {};
  el.innerHTML = `
    <div class="card">
      <h2>满舱水位摘要 <span class="muted">读轨：${cf.track || 'cutover_book'}</span></h2>
      <div class="stat-strip">
        <div class="stat"><span class="muted">满舱舱数</span><b>${cf.full_bin_count ?? 0}</b></div>
        <div class="stat"><span class="muted">满舱落点</span><b>${cf.gates_total ?? 0}</b></div>
        <div class="stat"><span class="muted">指纹偏移</span><b class="${cf.drifted ? 'chip bad' : 'chip ok'}">${cf.drifted ?? 0}</b></div>
        <div class="stat"><span class="muted">皮里对齐</span><b class="chip ${cf.aligned ? 'ok' : 'bad'}">${cf.aligned ? '已对齐' : '未对齐'}</b></div>
      </div>
      <table>
        <thead><tr><th>舱码</th><th>水位/容量</th><th>落点指纹</th><th>版本</th><th>落定时间</th></tr></thead>
        <tbody>
          ${(cf.gates || []).map((g) => `
            <tr>
              <td>${g.bin_code}</td>
              <td>${g.water_level}/${g.capacity}</td>
              <td class="muted">${(g.fingerprint || '').slice(0, 16)}…</td>
              <td>${g.revision}</td>
              <td>${g.committed_at}</td>
            </tr>`).join('') || '<tr><td colspan="5" class="empty">暂无满舱落点</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>隔离对账</h2>
      <div class="row"><button id="run">生成对账报告</button></div>
      <table>
        <thead><tr><th>时间</th><th>摘要</th><th>待复核</th><th>放行</th><th>回炉</th><th>废弃</th><th>错位</th></tr></thead>
        <tbody>
          ${(data.items || []).map((r) => `
            <tr>
              <td>${r.created_at}</td>
              <td>${r.summary}</td>
              <td>${r.open_rows}</td>
              <td>${r.released}</td>
              <td>${r.reworking}</td>
              <td>${r.abandoned}</td>
              <td><span class="chip ${r.mismatch ? 'bad' : 'ok'}">${r.mismatch}</span></td>
            </tr>`).join('') || '<tr><td colspan="7" class="muted">暂无报告</td></tr>'}
        </tbody>
      </table>
    </div>`;
  el.querySelector('#run').onclick = async () => {
    await api('/api/reports/run', { method: 'POST', body: '{}' });
    await renderReconcile(el);
  };
}
