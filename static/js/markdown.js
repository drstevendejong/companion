// markdown.js - Self-contained XSS-safe Markdown renderer (zero external dependencies)

export function renderMarkdown(raw) {
  if (!raw) return "";
  const lines = String(raw).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    const fenceMatch = line.match(/^```([\w-]*)\s*$/);
    if (fenceMatch) {
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].match(/^```\s*$/)) {
        codeLines.push(escapeHtml(lines[i]));
        i++;
      }
      i++; // skip closing ```
      out.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
      continue;
    }

    // Blank line
    if (!line.trim()) {
      i++;
      continue;
    }

    // Horizontal rule
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      out.push("<hr />");
      i++;
      continue;
    }

    // Headings
    const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      out.push(`<h${level}>${renderInline(headingMatch[2])}</h${level}>`);
      i++;
      continue;
    }

    // Blockquote
    if (line.startsWith(">")) {
      const quoteLines = [];
      while (i < lines.length && lines[i].startsWith(">")) {
        quoteLines.push(renderInline(lines[i].replace(/^>\s?/, "")));
        i++;
      }
      out.push(`<blockquote>${quoteLines.join("<br />")}</blockquote>`);
      continue;
    }

    // Markdown table
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
      const headers = splitTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      const thHtml = headers.map((h) => `<th>${renderInline(h)}</th>`).join("");
      const trHtml = rows
        .map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join("")}</tr>`)
        .join("");
      out.push(`<table><thead><tr>${thHtml}</tr></thead><tbody>${trHtml}</tbody></table>`);
      continue;
    }

    // Unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*[-*+]\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // Paragraph (collect consecutive non-block lines)
    const paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].match(/^```/) &&
      !lines[i].match(/^(#{1,4})\s+/) &&
      !lines[i].startsWith(">") &&
      !lines[i].match(/^\s*[-*+]\s+/) &&
      !lines[i].match(/^\s*\d+\.\s+/) &&
      !lines[i].match(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/)
    ) {
      paraLines.push(renderInline(lines[i]));
      i++;
    }
    out.push(`<p>${paraLines.join("<br />")}</p>`);
  }

  return out.join("\n");
}

function splitTableRow(row) {
  return row
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());
}

function renderInline(text) {
  // Split out inline `code` first so formatting inside backticks isn't mangled
  const segments = String(text).split(/(`[^`]+`)/g);
  return segments
    .map((seg) => {
      if (seg.startsWith("`") && seg.endsWith("`") && seg.length >= 2) {
        return `<code>${escapeHtml(seg.slice(1, -1))}</code>`;
      }
      let esc = escapeHtml(seg);
      esc = esc.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      esc = esc.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
      return esc;
    })
    .join("");
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
