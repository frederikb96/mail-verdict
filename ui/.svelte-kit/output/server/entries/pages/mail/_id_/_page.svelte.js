import "clsx";
import { M as MailList } from "../../../../chunks/MailList.js";
import { d as attr, a as attr_class, h as stringify, c as escape_html, e as ensure_array_like } from "../../../../chunks/index2.js";
import "../../../../chunks/stores.js";
function VerdictBadge($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    $$renderer2.push(`<div class="flex items-center gap-3">`);
    {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<span class="text-xs text-text-muted">No verdict</span>`);
    }
    $$renderer2.push(`<!--]--></div> `);
    {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]-->`);
  });
}
function AuthBadge($$renderer, $$props) {
  let { dkim, spf, dmarc } = $$props;
  function statusClass(val) {
    if (val === true) return "bg-ham";
    if (val === false) return "bg-spam";
    return "bg-text-muted";
  }
  function statusLabel(val) {
    if (val === true) return "pass";
    if (val === false) return "fail";
    return "n/a";
  }
  $$renderer.push(`<div class="flex items-center gap-3"><div class="flex items-center gap-1"${attr("title", `DKIM: ${stringify(statusLabel(dkim))}`)}><span${attr_class(`w-2 h-2 rounded-full ${stringify(statusClass(dkim))}`)}></span> <span class="text-[10px] text-text-muted">DKIM</span></div> <div class="flex items-center gap-1"${attr("title", `SPF: ${stringify(statusLabel(spf))}`)}><span${attr_class(`w-2 h-2 rounded-full ${stringify(statusClass(spf))}`)}></span> <span class="text-[10px] text-text-muted">SPF</span></div> <div class="flex items-center gap-1"${attr("title", `DMARC: ${stringify(statusLabel(dmarc))}`)}><span${attr_class(`w-2 h-2 rounded-full ${stringify(statusClass(dmarc))}`)}></span> <span class="text-[10px] text-text-muted">DMARC</span></div></div>`);
}
function MailDetail($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    let { mail } = $$props;
    let showHtml = false;
    function formatDate(dateStr) {
      if (!dateStr) return "";
      return new Date(dateStr).toLocaleString(void 0, {
        weekday: "short",
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit"
      });
    }
    function formatAddrs(addrs) {
      if (!addrs) return "";
      if (Array.isArray(addrs)) return addrs.join(", ");
      if (typeof addrs === "string") return addrs;
      return String(addrs);
    }
    function formatSize(bytes) {
      if (bytes === null) return "";
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }
    $$renderer2.push(`<div class="h-full flex flex-col overflow-hidden"><div class="p-4 border-b border-border space-y-3"><div class="flex items-start justify-between gap-3"><h1 class="text-lg font-medium text-text-primary leading-tight">${escape_html(mail.subject ?? "(no subject)")}</h1> `);
    if (mail.size_bytes) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<span class="text-[10px] text-text-muted whitespace-nowrap flex-shrink-0">${escape_html(formatSize(mail.size_bytes))}</span>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--></div> <div class="space-y-1 text-xs"><div class="flex gap-2"><span class="text-text-muted w-10">From</span> <span class="text-text-secondary">${escape_html(mail.from_addr ?? "(unknown)")}</span></div> <div class="flex gap-2"><span class="text-text-muted w-10">To</span> <span class="text-text-secondary">${escape_html(formatAddrs(mail.to_addrs))}</span></div> `);
    if (mail.cc_addrs) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="flex gap-2"><span class="text-text-muted w-10">Cc</span> <span class="text-text-secondary">${escape_html(formatAddrs(mail.cc_addrs))}</span></div>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> <div class="flex gap-2"><span class="text-text-muted w-10">Date</span> <span class="text-text-secondary">${escape_html(formatDate(mail.received_at))}</span></div></div> <div class="flex items-center justify-between gap-4 pt-1">`);
    AuthBadge($$renderer2, {
      dkim: mail.dkim_pass,
      spf: mail.spf_pass,
      dmarc: mail.dmarc_pass
    });
    $$renderer2.push(`<!----> `);
    VerdictBadge($$renderer2, {
      mailId: mail.id,
      accountId: mail.account_id
    });
    $$renderer2.push(`<!----></div> `);
    if (mail.tags.length > 0) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="flex items-center gap-1.5 pt-1"><!--[-->`);
      const each_array = ensure_array_like(mail.tags);
      for (let $$index = 0, $$length = each_array.length; $$index < $$length; $$index++) {
        let tag = each_array[$$index];
        $$renderer2.push(`<span class="px-1.5 py-0.5 rounded text-[10px] bg-surface-light text-text-muted">${escape_html(tag.tag_name)}</span>`);
      }
      $$renderer2.push(`<!--]--></div>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> `);
    if (mail.attachments.length > 0) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="flex items-center gap-2 pt-1"><svg class="w-3.5 h-3.5 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"></path></svg> <!--[-->`);
      const each_array_1 = ensure_array_like(mail.attachments);
      for (let $$index_1 = 0, $$length = each_array_1.length; $$index_1 < $$length; $$index_1++) {
        let att = each_array_1[$$index_1];
        $$renderer2.push(`<span class="text-[10px] text-text-secondary">${escape_html(att.filename ?? "attachment")}</span>`);
      }
      $$renderer2.push(`<!--]--></div>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--></div> `);
    if (mail.body_html) {
      $$renderer2.push("<!--[-->");
      $$renderer2.push(`<div class="px-4 py-1.5 border-b border-border flex gap-2"><button${attr_class("text-[11px] px-2 py-0.5 rounded transition-colors", void 0, {
        "bg-surface-light": !showHtml,
        "text-text-primary": !showHtml,
        "text-text-muted": showHtml
      })}>Text</button> <button${attr_class("text-[11px] px-2 py-0.5 rounded transition-colors", void 0, {
        "bg-surface-light": showHtml,
        "text-text-primary": showHtml,
        "text-text-muted": !showHtml
      })}>HTML</button></div>`);
    } else {
      $$renderer2.push("<!--[!-->");
    }
    $$renderer2.push(`<!--]--> <div class="flex-1 overflow-y-auto p-4">`);
    if (mail.body_text) {
      $$renderer2.push("<!--[1-->");
      $$renderer2.push(`<pre class="text-sm text-text-secondary whitespace-pre-wrap font-sans leading-relaxed">${escape_html(mail.body_text)}</pre>`);
    } else {
      $$renderer2.push("<!--[!-->");
      $$renderer2.push(`<p class="text-sm text-text-muted">No body content</p>`);
    }
    $$renderer2.push(`<!--]--></div></div>`);
  });
}
function _page($$renderer, $$props) {
  $$renderer.component(($$renderer2) => {
    let { data } = $$props;
    $$renderer2.push(`<div class="h-full flex"><div class="w-96 flex-shrink-0 border-r border-border hidden lg:block">`);
    MailList($$renderer2);
    $$renderer2.push(`<!----></div> <div class="flex-1 min-w-0">`);
    MailDetail($$renderer2, { mail: data.mail });
    $$renderer2.push(`<!----></div></div>`);
  });
}
export {
  _page as default
};
