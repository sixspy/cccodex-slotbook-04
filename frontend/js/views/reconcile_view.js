export const reconcileViewTemplate = `
  <div v-show="tab===4" class="panel">
    <h2>对账互证</h2>
    <p :class="recon.all_ok ? 'ok' : 'err'">{{ recon.all_ok ? '全部同源' : '存在分裂' }}</p>
    <table>
      <thead><tr><th>档</th><th>盘面已订</th><th>正式册席</th><th>已晋升</th><th>晋升占位</th><th>双态</th><th>余席</th><th>审计</th><th>结果</th></tr></thead>
      <tbody>
        <tr v-for="i in (recon.items||[])" :key="i.slot_code">
          <td>{{ i.slot_code }}</td><td>{{ i.slot_booked }}</td>
          <td>{{ i.booking_seats }}</td><td>{{ i.promoted_waits }}</td>
          <td>{{ i.promotion_holds }}</td>
          <td :class="i.dual_state ? 'err' : 'ok'">{{ i.dual_state }}</td>
          <td>{{ i.free_seats }}</td>
          <td :class="i.audit_ok ? 'ok' : 'err'">{{ i.audit_ok ? '齐' : '缺' }}</td>
          <td :class="i.ok?'ok':'err'">{{ i.ok ? '通过' : '分裂' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
`;
