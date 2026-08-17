import { Infographic } from '@antv/infographic';
import { Resvg } from '@resvg/resvg-js';
import { DOMParser } from 'linkedom';
import { setupDOM } from './node_modules/@antv/infographic/esm/ssr/dom-shim.js';

function numberAttr(element, name) {
  const value = Number.parseFloat(element.getAttribute(name) || '0');
  return Number.isFinite(value) ? value : 0;
}

function normalizeSVG(source) {
  const withoutInstructions = source
    .replace(/<\?xml[^>]*\?>/gi, '')
    .replace(/<\?xml-stylesheet[^>]*\?>/gi, '')
    .trim();
  const document = new DOMParser().parseFromString(withoutInstructions, 'image/svg+xml');
  const root = document.documentElement;
  root.setAttribute('font-family', 'Inter, Arial, sans-serif');

  for (const foreignObject of [...root.querySelectorAll('foreignObject')]) {
    const span = foreignObject.querySelector('span');
    if (!span) {
      foreignObject.remove();
      continue;
    }
    const x = numberAttr(foreignObject, 'x');
    const y = numberAttr(foreignObject, 'y');
    const width = numberAttr(foreignObject, 'width');
    const height = numberAttr(foreignObject, 'height');
    const style = span.style;
    const horizontal = style.justifyContent || style.textAlign || 'start';
    const vertical = style.alignItems || style.alignContent || 'center';
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    const centered = horizontal === 'center';
    const ended = horizontal === 'flex-end' || horizontal === 'right' || horizontal === 'end';
    text.setAttribute('x', String(centered ? x + width / 2 : ended ? x + width : x));
    text.setAttribute('text-anchor', centered ? 'middle' : ended ? 'end' : 'start');
    if (vertical === 'flex-end' || vertical === 'end') {
      text.setAttribute('y', String(y + height));
      text.setAttribute('dominant-baseline', 'text-after-edge');
    } else if (vertical === 'flex-start' || vertical === 'start') {
      text.setAttribute('y', String(y));
      text.setAttribute('dominant-baseline', 'text-before-edge');
    } else {
      text.setAttribute('y', String(y + height / 2));
      text.setAttribute('dominant-baseline', 'central');
    }
    text.setAttribute('fill', style.color || '#262626');
    text.setAttribute('font-size', style.fontSize || '14px');
    text.setAttribute('font-weight', style.fontWeight || 'normal');
    text.textContent = span.textContent || '';
    foreignObject.replaceWith(text);
  }
  for (const use of [...root.querySelectorAll('use')]) {
    const x = numberAttr(use, 'x');
    const y = numberAttr(use, 'y');
    const width = numberAttr(use, 'width') || 20;
    const height = numberAttr(use, 'height') || 20;
    const mark = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', String(x + width / 2));
    circle.setAttribute('cy', String(y + height / 2));
    circle.setAttribute('r', String(Math.max(2, Math.min(width, height) * 0.42)));
    circle.setAttribute('fill', '#22c55e');
    const check = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    check.setAttribute(
      'd',
      `M ${x + width * 0.28} ${y + height * 0.52} L ${x + width * 0.44} ${y + height * 0.68} L ${x + width * 0.74} ${y + height * 0.34}`,
    );
    check.setAttribute('fill', 'none');
    check.setAttribute('stroke', 'white');
    check.setAttribute('stroke-width', String(Math.max(1.5, width * 0.09)));
    check.setAttribute('stroke-linecap', 'round');
    check.setAttribute('stroke-linejoin', 'round');
    mark.append(circle, check);
    use.replaceWith(mark);
  }
  return root.outerHTML;
}

function renderImmediately(syntax) {
  const { document } = setupDOM();
  const container = document.getElementById('container');
  const infographic = new Infographic({ container, editable: false });
  let renderError;
  infographic.on('error', (error) => {
    renderError = error;
  });
  infographic.render(syntax);
  if (renderError || !infographic.node) {
    throw renderError || new Error('AntV could not render the infographic syntax');
  }
  const svg = infographic.node.outerHTML;
  infographic.destroy();
  return svg;
}

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);

try {
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const syntax = String(input.syntax || '');
  // AntV's normal SSR helper waits for remote font and icon resources. The
  // harness deliberately renders the synchronous local SVG tree instead,
  // then replaces resource-backed icons and HTML text with native SVG nodes.
  const svg = normalizeSVG(renderImmediately(syntax));
  const result = { svg };
  if (input.include_png) {
    const renderer = new Resvg(svg, {
      fitTo: { mode: 'width', value: 1400 },
      background: 'rgba(255, 255, 255, 1)',
    });
    result.png_base64 = renderer.render().asPng().toString('base64');
  }
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  process.stderr.write(String(error?.stack || error));
  process.exitCode = 1;
}
