"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { findMatchingBrace } = require("../../scripts/patches/lib/minified-js.js");
const { listWebviewScriptAssets } = require("../../scripts/patches/lib/assets.js");
const { patchBreaksJavaScriptSyntax } = require("../../scripts/patches/lib/javascript-syntax.js");

const IDENT = "[A-Za-z_$][\\w$]*";
const FULL_LOCAL_MARKER = "codexLinuxHydexFullLocalCreditGate";
const FULL_LOCAL_SUBMIT_MARKER = "codexLinuxHydexFullLocalSubmitGate";
const REQUEST_MARKER = "codexLinuxHydexOffloadRequest";
const QUEUE_MARKER = "codexLinuxHydexOffloadQueue";
const CONTROL_MARKER = "codexLinuxHydexOffloadControl";
const NEXT_TURN_MARKER = "codexLinuxHydexOffloadNextTurn";
const PRODUCT_MODE_LABEL_MARKER = "codexLinuxHydexProductModeLabel";
const PRODUCT_MODE_SELECTED_LABEL_MARKER = "codexLinuxHydexProductModeSelectedLabel";
const PRODUCT_MODE_LOCALE_LABEL_MARKER = "codexLinuxHydexProductModeLocaleLabel";
const HYDEX_PRODUCT_NAME = "Hydex";
const PRODUCT_MODE_LABEL_SOURCE =
  'id:"sidebarElectron.productMode.codex",defaultMessage:"Codex"';
const PRODUCT_MODE_LABEL_PATCHED =
  `id:"sidebarElectron.productMode.codex",defaultMessage:"Hydex"/*${PRODUCT_MODE_LABEL_MARKER}*/`;
const PRODUCT_MODE_LOCALE_KEY = '"sidebarElectron.productMode.codex":';
const PRODUCT_MODE_LOCALE_MARKER_COMMENT = `/*${PRODUCT_MODE_LOCALE_LABEL_MARKER}*/`;

function enclosingFunction(source, position, maxLength = 80_000) {
  const offset = Math.max(0, position - maxLength);
  const headers = [...source.slice(offset, position + 1).matchAll(/\bfunction(?: [A-Za-z_$][\w$]*)?\(([^)]*)\)\{/g)];
  for (const header of headers.reverse()) {
    const start = offset + header.index, open = start + header[0].length - 1;
    const end = findMatchingBrace(source, open);
    if (end >= position && end - start <= maxLength) return {start, end: end + 1, open,
      opening: source.slice(start, open + 1), argument: header[1], region: source.slice(start, end + 1)};
  }
  return null;
}

function localScopeNames(source) {
  return [...new Set([...source.matchAll(new RegExp(`(?:(${IDENT})\\.value\\.kind===[\"\x60]local[\"\x60]|[\"\x60]local[\"\x60]===(${IDENT})\\.value\\.kind)`, "g"))].map(m => m[1] || m[2]))];
}

function nextTurnUpdaterMatches(source) {
  const hook = new RegExp(`\\{modelSettings:(${IDENT}),setDefaultModelAndReasoningEffort:(${IDENT}),setModelAndReasoningEffort:(${IDENT}),setModelAndReasoningEffortForNextTurn:(${IDENT})\\}=`, "g");
  return [...source.matchAll(hook)].flatMap(match => {
    const owner = enclosingFunction(source, match.index);
    if (!owner) return [];
    const returned = new RegExp(`\\{modelSettings:${match[1]},setDefaultModelAndReasoningEffort:${match[2]},setModelAndReasoningEffort:${match[3]},selectComposerModelAndReasoningEffort:(${IDENT})\\}`, "g");
    const results = [...owner.region.matchAll(returned)];
    return results.length === 1 ? [{start: owner.start + results[0].index, original: results[0][0], setter: match[4]}] : [];
  });
}

function matchesHydexNextTurnUpdaterContract(source) {
  return source.includes(NEXT_TURN_MARKER) || nextTurnUpdaterMatches(source).length === 1;
}

function applyHydexNextTurnUpdaterPatch(source) {
  if (source.includes(NEXT_TURN_MARKER)) return source;
  const matches = nextTurnUpdaterMatches(source);
  if (matches.length !== 1) { warn(`Expected one next-turn updater owner, found ${matches.length}`, "Hydex next-turn updater"); return source; }
  const m = matches[0], replacement = m.original.slice(0,-1) + `,/*${NEXT_TURN_MARKER}*/setModelAndReasoningEffortForNextTurn:${m.setter}}`;
  return source.slice(0,m.start) + replacement + source.slice(m.start+m.original.length);
}

function patchMatchingAssets(extractedDir, candidate, patch, description) {
  const dir=path.join(extractedDir,"webview/assets"), pending=[];
  for (const name of listWebviewScriptAssets(extractedDir)) {
    const file=path.join(dir,name),source=fs.readFileSync(file,"utf8");
    if (!candidate(source)) continue;
    const result=patch(source);
    if (result===source && !source.includes(NEXT_TURN_MARKER) && !source.includes(FULL_LOCAL_SUBMIT_MARKER)) throw Error(`Unresolved ${description} contract in ${name}`);
    if (result!==source && patchBreaksJavaScriptSyntax(source,result)) throw Error(`Invalid ${description} syntax in ${name}`);
    pending.push({file,name,source,result});
  }
  if (!pending.length) throw Error(`Could not find ${description} assets`);
  for (const item of pending) if (item.result!==item.source) fs.writeFileSync(item.file,item.result);
  return {matched:pending.length,changed:pending.some(item=>item.source!==item.result),targets:pending.map(item=>item.name)};
}

function applyHydexNextTurnUpdaterAssets(extractedDir) {
  return patchMatchingAssets(extractedDir,source=>source.includes(NEXT_TURN_MARKER)||/\{modelSettings:[\w$]+,setDefaultModelAndReasoningEffort:[\w$]+,setModelAndReasoningEffort:[\w$]+,setModelAndReasoningEffortForNextTurn:[\w$]+\}=/.test(source),source=>{
    if(!matchesHydexNextTurnUpdaterContract(source)) throw Error("Ambiguous next-turn updater owner");
    return applyHydexNextTurnUpdaterPatch(source);
  },"model selection next-turn updater");
}

function applyHydexFullLocalSubmitGateAssets(extractedDir) {
  return patchMatchingAssets(extractedDir,source=>source.includes("localFollowUpPreparation:")&&source.includes("isNativeWork:")&&source.includes("submitDisabled:")&&/\b[\w$]+=(?:[\w$]+|\(0,[\w$]+\.[\w$]+\))\((?:"local"===[\w$]+|[\w$]+===`local`)&&[\w$]+(?:\.[\w$]+)?\)/.test(source),source=>{
    if(!matchesHydexFullLocalSubmitGateContract(source)) throw Error("Ambiguous local submit-admission owner");
    return applyHydexFullLocalSubmitGatePatch(source);
  },"local Full submit admission");
}

function applyHydexProductNamePatch(extractedDir) {
  const packagePath = path.join(extractedDir, "package.json");
  const source = fs.readFileSync(packagePath, "utf8");
  const value = JSON.parse(source);
  if (value.productName === HYDEX_PRODUCT_NAME) {
    return { changed: false, target: "package.json" };
  }
  if (value.productName !== "Codex") {
    throw new Error(
      `Expected Electron productName Codex, found ${JSON.stringify(value.productName)}`,
    );
  }
  value.productName = HYDEX_PRODUCT_NAME;
  fs.writeFileSync(packagePath, `${JSON.stringify(value, null, 2)}\n`);
  return { changed: true, target: "package.json" };
}

function exactCount(source, needle) {
  return source.split(needle).length - 1;
}

function productModeLabelContract(source) {
  return {
    original: exactCount(source, PRODUCT_MODE_LABEL_SOURCE),
    patched: exactCount(source, PRODUCT_MODE_LABEL_PATCHED),
  };
}

function matchesHydexProductModeLabelContract(source) {
  const { original, patched } = productModeLabelContract(source);
  return (original === 1 && patched === 0) || (original === 0 && patched === 1);
}

function applyHydexProductModeLabelPatch(source) {
  const { original, patched } = productModeLabelContract(source);
  if (original === 0 && patched === 1) return source;
  if (original !== 1 || patched !== 0) {
    warn(
      `Expected one current Codex product-mode label, found ${original} original and ${patched} patched`,
      "Hydex product-mode label patch",
    );
    return source;
  }
  return source.replace(PRODUCT_MODE_LABEL_SOURCE, PRODUCT_MODE_LABEL_PATCHED);
}

function productModeSelectedLabelPattern() {
  return new RegExp(`if\\("codex"===${IDENT}\\)\\{(?:(?!}else).){0,1500}?` +
    `(\\(0,(${IDENT})\\.jsx\\)\\(${IDENT},\\{"aria-hidden":"true",className:"h-\\[1em\\] w-auto overflow-visible","data-no-autosize":!0\\}\\))`, "g");
}

function productModeSelectedLabelContract(source) {
  return {
    original: [...source.matchAll(productModeSelectedLabelPattern())].length,
    patched: exactCount(source, PRODUCT_MODE_SELECTED_LABEL_MARKER),
  };
}

function matchesHydexProductModeSelectedLabelContract(source) {
  const { original, patched } = productModeSelectedLabelContract(source);
  return (original === 1 && patched === 0) || (original === 0 && patched === 1);
}

function applyHydexProductModeSelectedLabelPatch(source) {
  const matches = [...source.matchAll(productModeSelectedLabelPattern())];
  const patched = exactCount(source, PRODUCT_MODE_SELECTED_LABEL_MARKER);
  if (matches.length === 0 && patched === 1) return source;
  if (matches.length !== 1 || patched !== 0) {
    warn(
      `Expected one selected Codex product-mode wordmark, found ${matches.length} original and ${patched} patched`,
      "Hydex selected product-mode label patch",
    );
    return source;
  }

  const [match] = matches;
  const original = match[1];
  const jsxAlias = match[2];
  const originalIndex = match.index + match[0].indexOf(original);
  const replacement =
    `(0,${jsxAlias}.jsx)(\`span\`,{"aria-hidden":\`true\`,` +
    `className:\`font-openai-sans\`,children:\`Hydex\`})` +
    `/*${PRODUCT_MODE_SELECTED_LABEL_MARKER}*/`;
  return source.slice(0, originalIndex) +
    replacement +
    source.slice(originalIndex + original.length);
}

function applyHydexProductModeLocalePatch(extractedDir) {
  const dir = path.join(extractedDir,"webview/assets"), matches=[];
  for (const name of listWebviewScriptAssets(extractedDir)) {
    const assetPath=path.join(dir,name), source=fs.readFileSync(assetPath,"utf8");
    const count=exactCount(source,PRODUCT_MODE_LOCALE_KEY);
    if (!count) continue;
    const start=source.indexOf(PRODUCT_MODE_LOCALE_KEY)+PRODUCT_MODE_LOCALE_KEY.length;
    const literal=source.slice(start).match(/^"(?:[^"\\]|\\.)*"/);
    if (count!==1 || !literal) throw Error(`Unexpected Codex locale contract in ${name}`);
    const value=JSON.parse(literal[0]), end=start+literal[0].length;
    const marked=source.startsWith(PRODUCT_MODE_LOCALE_MARKER_COMMENT,end);
    if (marked && value!==HYDEX_PRODUCT_NAME) throw Error(`Invalid Hydex locale marker in ${name}`);
    matches.push({assetPath,source,start,end,name,marked});
  }
  if (!matches.length) throw Error("Could not find Codex product-mode locale catalogs");
  for (const m of matches) if (!m.marked) fs.writeFileSync(m.assetPath,m.source.slice(0,m.start)+JSON.stringify(HYDEX_PRODUCT_NAME)+PRODUCT_MODE_LOCALE_MARKER_COMMENT+m.source.slice(m.end));
  return {changed:matches.some(m=>!m.marked),matched:matches.length,targets:matches.map(m=>m.name).sort()};
}

function warn(message, patchName) {
  console.warn(`WARN: ${message} - skipping ${patchName}`);
}

function requestBridgePattern() {
  return new RegExp(`async sendRequest\\((${IDENT}),(${IDENT}),(${IDENT})\\)\\{if\\((?:null==this\\.dispatchMessage|this\\.dispatchMessage==null)\\)throw Error\\((?:"AppServerRequestClient is missing a message dispatcher"|\x60AppServerRequestClient is missing a message dispatcher\x60)\\);`,"g");
}

function requestBridgeMatches(source) {
  return [...source.matchAll(requestBridgePattern())];
}

function matchesHydexRequestBridgeContract(source) {
  return source.includes(REQUEST_MARKER)
    ? source.includes("codexLinuxHydexFullLocalConfigured") && source.includes("force_full") &&
      source.includes(QUEUE_MARKER)
    : requestBridgeMatches(source).length === 1;
}

function applyHydexRequestBridgePatch(source) {
  if (source.includes(REQUEST_MARKER)) return source;

  const matches = requestBridgeMatches(source);
  if (matches.length !== 1) {
    warn(
      `Expected one app-server request bridge, found ${matches.length}`,
      "Hydex offload request bridge patch",
    );
    return source;
  }

  const [match] = matches;
  const methodVar = match[1];
  const paramsVar = match[2];
  const injection =
    // Queue add can start an idle thread immediately. Apply routing first; unknown
    // override fields on queue requests would otherwise be silently ignored.
    `if((this.hostId==null||this.hostId===\`local\`)&&` +
    `(${methodVar}===\`thread/queue/add\`||${methodVar}===\`thread/queue/update\`||${methodVar}===\`thread/queue/start\`)&&` +
    `typeof ${paramsVar}?.threadId===\`string\`){` +
    `await this.sendRequest(\`thread/settings/update\`,{threadId:${paramsVar}.threadId})}/*${QUEUE_MARKER}*/` +
    `if((${methodVar}===\`turn/start\`||${methodVar}===\`thread/settings/update\`)&&${paramsVar}!=null){` +
    `let r=null;try{let e=localStorage.getItem(\`hydex.offloadOverride\`);` +
    `r=e===\`force_on\`||e===\`force_full\`||e===\`force_off\`?e:null}catch{}` +
    `if(r!==\`force_full\`||this.hostId==null||this.hostId===\`local\`){` +
    `${paramsVar}=${methodVar}===\`thread/settings/update\`&&${paramsVar}?.threadSettings!=null?` +
    `{...${paramsVar},threadSettings:{...${paramsVar}.threadSettings,modelOffloadOverride:r}}:` +
    `{...${paramsVar},modelOffloadOverride:r}}}/*${REQUEST_MARKER}*/` +
    `if(this.hostId===\`local\`&&${methodVar}===\`config/read\`){let v=await this.sendConfigReadRequest(${paramsVar},${match[3]});` +
    `globalThis.codexLinuxHydexFullLocalConfigured=v?.config?.model_offload?.enabled===!0&&v?.config?.model_offload?.mode===\`full\`;` +
    `try{window.dispatchEvent(new Event(\`hydex-offload-change\`))}catch{}return v}` +
    `if(this.hostId===\`local\`&&(${methodVar}===\`thread/start\`||${methodVar}===\`thread/resume\`||${methodVar}===\`thread/fork\`)&&${paramsVar}!=null){` +
    `let f=null;try{f=localStorage.getItem(\`hydex.offloadOverride\`)}catch{}` +
    `if(f===\`force_full\`||(f!==\`force_on\`&&f!==\`force_off\`&&globalThis.codexLinuxHydexFullLocalConfigured===!0))` +
    `${paramsVar}={...${paramsVar},config:{...${paramsVar}.config,"model_offload.enabled":!0,"model_offload.mode":\`full\`}}}`;

  return source.slice(0, match.index + match[0].length) +
    injection +
    source.slice(match.index + match[0].length);
}

function composerSignaturePattern() {
  return new RegExp(`function (${IDENT})\\((${IDENT})\\)\\{let [^;{}]{0,1500}?(${IDENT})=\\(0,(${IDENT})\\.c\\)\\(\\d+\\),\\{([^{}]+)\\}=\\2`,"g");
}

function composerContractMatches(source) {
  return [...source.matchAll(composerSignaturePattern())].flatMap(match=>{
    const props=match[5].split(",").map(item=>item.split(":"));
    const allowed=new Set(["serviceTierDefaults","allowAeonDraftModelSelection","conversationId","hasThreadResumeFailed","hideLabel","home","isCodexHome","permissionsCwdOverride","permissionsHostId"]);
    if (props.some(parts=>parts.length!==2 || !allowed.has(parts[0]) || !new RegExp(`^${IDENT}$`).test(parts[1])) || new Set(props.map(p=>p[0])).size!==props.length) return [];
    const values=Object.fromEntries(props);
    if (!(values.serviceTierDefaults||values.allowAeonDraftModelSelection) || !["conversationId","hideLabel","permissionsCwdOverride","permissionsHostId"].every(key=>values[key])) return [];
    const end=findMatchingBrace(source,source.indexOf("{",match.index));
    if (end<0 || end-match.index>80_000) return [];
    const region=source.slice(match.index,end+1);
    if (!region.includes("composer.intelligenceDropdown.tooltip") || !region.includes("selectComposerModelAndReasoningEffort")) return [];
    return [{index:match.index,end:end+1,region,props:values}];
  });
}

function matchesHydexComposerContract(source) {
  return source.includes(CONTROL_MARKER)
    ? source.includes(`const ${CONTROL_MARKER}=(function(codexLinuxHydexReact,codexLinuxHydexJsx){return function ${CONTROL_MARKER}(`) &&
      source.includes('"data-value":`force_full`') && source.includes("Hydex full")
    : composerContractMatches(source).length === 1;
}

function uniqueAlias(region, method) {
  const matches = [
    ...region.matchAll(new RegExp(`\\(0,(${IDENT})\\.${method}\\)\\(`, "g")),
  ];
  const aliases = [...new Set(matches.map((match) => match[1]))];
  return aliases.length === 1 ? aliases[0] : null;
}

function applyHydexComposerControlPatch(source) {
  if (source.includes(CONTROL_MARKER)) return source;
  const originalSource=source;
  const initial=composerContractMatches(source);
  if(initial.length!==1) {warn(`Expected one local Codex model picker, found ${initial.length}`,"Hydex offload composer control patch");return source;}
  if (nextTurnUpdaterMatches(source).length===1) source=applyHydexNextTurnUpdaterPatch(source);
  const matches=composerContractMatches(source);
  if (matches.length!==1) {warn(`Expected one local Codex model picker, found ${matches.length}`,"Hydex offload composer control patch");return source;}
  const signature=matches[0],functionStart=signature.index,functionEnd=signature.end,region=signature.region;
  const reactAlias=uniqueAlias(region,"useState");
  const aliases=[...new Set([...region.matchAll(new RegExp(`\\(0,(${IDENT})\\.(?:jsx|jsxs)\\)\\(`,"g"))].map(m=>m[1]))];
  const jsxAlias=aliases.length===1?aliases[0]:null;
  const selection=region.match(new RegExp(`let (${IDENT})=(?:\\(0,${IDENT}\\.${IDENT}\\)|${IDENT})\\(${IDENT}\\),\\{modelSettings:(${IDENT}),selectComposerModelAndReasoningEffort:${IDENT},setDefaultModelAndReasoningEffort:${IDENT},setModelAndReasoningEffort:${IDENT}\\}=\\1`));
  const tail=region.match(new RegExp(`,(${IDENT})\\}$`));
  if (!reactAlias || !jsxAlias || !selection || !tail || !new RegExp(`${tail?.[1]}=\\(0,${jsxAlias}\\.(?:jsx|jsxs)\\)\\(`).test(region)) {warn("Could not resolve local model picker symbols","Hydex offload composer control patch");return originalSource;}
  const conversationVar=signature.props.conversationId,hostVar=signature.props.permissionsHostId,modelSelectionVar=selection[1],modelSettingsVar=selection[2],resultVar=tail[1],originalTail=tail[0];
  const helper =
    `/*${CONTROL_MARKER}*/` +
    `function codexLinuxHydexOffloadRead(){try{let e=localStorage.getItem(\`hydex.offloadOverride\`);` +
    `return e===\`force_on\`||e===\`force_full\`||e===\`force_off\`?e:\`auto\`}catch{return\`auto\`}}` +
    `const ${CONTROL_MARKER}=(function(codexLinuxHydexReact,codexLinuxHydexJsx){return function ${CONTROL_MARKER}({onApply:e}){let[t,n]=(0,codexLinuxHydexReact.useState)(codexLinuxHydexOffloadRead),` +
    `[r,i]=(0,codexLinuxHydexReact.useState)(!1),a=(0,codexLinuxHydexReact.useRef)(null),` +
    `o=(0,codexLinuxHydexReact.useRef)(null),s=t===\`force_full\`?\`Hydex full\`:t===\`force_on\`?\`Hydex on\`:t===\`force_off\`?\`Hydex off\`:\`Hydex auto\`,` +
    `c=c=>{let l=c.currentTarget.dataset.value;if(l!==\`auto\`&&l!==\`force_on\`&&l!==\`force_full\`&&l!==\`force_off\`)return;` +
    `n(l);try{l===\`auto\`?localStorage.removeItem(\`hydex.offloadOverride\`):` +
    `localStorage.setItem(\`hydex.offloadOverride\`,l);window.dispatchEvent(new Event(\`hydex-offload-change\`))}catch{}let u=l===\`auto\`?null:l;` +
    `try{Promise.resolve(e?.(u)).catch(()=>{})}catch{}o.current?.hidePopover(),a.current?.focus()},` +
    `l=()=>{let e=a.current,t=o.current;if(e==null||t==null)return;` +
    `if(t.matches(\`:popover-open\`)){t.hidePopover();return}let n=e.getBoundingClientRect();` +
    `t.style.left=Math.max(8,Math.min(n.left,window.innerWidth-168))+\`px\`,` +
    `t.style.bottom=Math.max(8,window.innerHeight-n.top+8)+\`px\`,t.showPopover(),` +
    `requestAnimationFrame(()=>t.querySelector(\`[aria-checked=\"true\"]\`)?.focus())},` +
    `u=e=>{if(e.key!==\`ArrowDown\`&&e.key!==\`ArrowUp\`)return;` +
    `let t=[...e.currentTarget.querySelectorAll(\`[role=\"menuitemradio\"]\`)];if(t.length===0)return;` +
    `let n=t.indexOf(document.activeElement),r=e.key===\`ArrowUp\`?-1:1,i=n<0?r>0?-1:0:n;` +
    `e.preventDefault(),t[(i+r+t.length)%t.length]?.focus()},` +
    `d=e=>i(e.currentTarget.matches(\`:popover-open\`)),f=\`flex w-full cursor-interaction items-center ` +
    `justify-between rounded-lg px-3 py-2 text-left text-sm text-default outline-none ` +
    `hover:bg-primary-ghost-hover focus-visible:bg-primary-ghost-hover\`;` +
    `(0,codexLinuxHydexReact.useEffect)(()=>{let e=()=>n(codexLinuxHydexOffloadRead());e();` +
    `window.addEventListener(\`storage\`,e);window.addEventListener(\`hydex-offload-change\`,e);` +
    `return()=>{window.removeEventListener(\`storage\`,e);window.removeEventListener(\`hydex-offload-change\`,e)}},[]);` +
    `return(0,codexLinuxHydexJsx.jsxs)(\`span\`,{className:\`relative inline-flex\`,children:[` +
    `(0,codexLinuxHydexJsx.jsxs)(\`button\`,{ref:a,type:\`button\`,\"aria-haspopup\":\`menu\`,\"aria-expanded\":r,` +
    `\"aria-label\":\`Hydex offload\`,title:\`Hydex offload\`,onClick:l,` +
    `className:\`h-token-button-composer flex max-w-[108px] shrink-0 cursor-interaction items-center ` +
    `justify-between gap-1 rounded-lg border border-default bg-primary px-2 text-sm text-default ` +
    `outline-none hover:bg-primary-ghost-hover focus-visible:ring-2 focus-visible:ring-ring\`,` +
    `children:[(0,codexLinuxHydexJsx.jsx)(\`span\`,{className:\`truncate\`,children:s}),` +
    `(0,codexLinuxHydexJsx.jsx)(\`span\`,{\"aria-hidden\":!0,className:\`text-tertiary\`,children:\`⌄\`})]}),` +
    `(0,codexLinuxHydexJsx.jsxs)(\`div\`,{ref:o,popover:\`auto\`,role:\`menu\`,\"aria-label\":\`Hydex offload\`,` +
    `onKeyDown:u,onToggle:d,className:\`min-w-40 rounded-xl bg-surface-elevated-secondary p-1 ` +
    `text-default shadow-lg ring-1 ring-border\`,style:{position:\`fixed\`,inset:\`auto\`,margin:0},children:[` +
    `(0,codexLinuxHydexJsx.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`auto\`,` +
    `\"data-value\":\`auto\`,onClick:c,className:f+(t===\`auto\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex auto\`,t===\`auto\`?(0,codexLinuxHydexJsx.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]}),` +
    `(0,codexLinuxHydexJsx.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`force_on\`,` +
    `\"data-value\":\`force_on\`,onClick:c,className:f+(t===\`force_on\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex on\`,t===\`force_on\`?(0,codexLinuxHydexJsx.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]}),` +
    `(0,codexLinuxHydexJsx.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`force_full\`,` +
    `\"data-value\":\`force_full\`,onClick:c,className:f+(t===\`force_full\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex full\`,t===\`force_full\`?(0,codexLinuxHydexJsx.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]}),` +
    `(0,codexLinuxHydexJsx.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`force_off\`,` +
    `\"data-value\":\`force_off\`,onClick:c,className:f+(t===\`force_off\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex off\`,t===\`force_off\`?(0,codexLinuxHydexJsx.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]})]})]})}` +
    `})(${reactAlias},${jsxAlias});`;
  const onApply =
    `${conversationVar}==null?void 0:e=>${modelSelectionVar}.setModelAndReasoningEffortForNextTurn(` +
    `${modelSettingsVar}.model,${modelSettingsVar}.reasoningEffort,` +
    `{threadSettings:{modelOffloadOverride:e}})`;
  const replacementTail =
    `,(0,${jsxAlias}.jsxs)(\`span\`,{className:\`inline-flex min-w-0 items-center gap-1\`,` +
    `children:[${resultVar},${hostVar}==null||${hostVar}===\`local\`?` +
    `(0,${jsxAlias}.jsx)(${CONTROL_MARKER},{onApply:${onApply}}):null]})}`;
  const patchedRegion = region.slice(0, -originalTail.length) + replacementTail;
  return source.slice(0,functionStart)+helper+patchedRegion+source.slice(functionEnd);
}


function fullLocalCreditGateMatches(source) {
  const pattern=new RegExp(`(${IDENT})=\\{imageGenerationLimit:${IDENT},isUsageBannerEnabled:${IDENT},onDismissImageGenerationLimit:${IDENT},rateLimit:${IDENT},rateLimitWarningThreshold:${IDENT},sendBlocked:${IDENT}\\}`,"g");
  return [...source.matchAll(pattern)].flatMap(match=>{
    const owner=enclosingFunction(source,match.index,12_000);
    if (!owner || !owner.region.endsWith(`,${match[1]}}`)) return [];
    const scopes=localScopeNames(owner.region),host=owner.region.match(new RegExp(`(?:"local"===(${IDENT})|(${IDENT})===\x60local\x60)&&${IDENT}\\|\\|`)),react=uniqueAlias(owner.region,"useEffect");
    if (scopes.length!==1 || !host || !react) return [];
    return [{...owner,result:match[1],tail:`,${match[1]}}`,scope:scopes[0],host:host[1]||host[2],react}];
  });
}

function matchesHydexFullLocalCreditGateContract(source) {
  return source.includes(FULL_LOCAL_MARKER) || fullLocalCreditGateMatches(source).length === 1;
}

function fullLocalPreferenceHelpers(marker) {
  return `/*${marker}*/` +
    `function codexLinuxHydexFullLocalRead(){try{let e=localStorage.getItem(\`hydex.offloadOverride\`);if(e===\`force_full\`)return!0;if(e===\`force_on\`||e===\`force_off\`)return!1}catch{}return globalThis.codexLinuxHydexFullLocalConfigured===!0}` +
    `function codexLinuxHydexFullLocalSubscribe(e){window.addEventListener(\`storage\`,e);window.addEventListener(\`hydex-offload-change\`,e);` +
    `return()=>{window.removeEventListener(\`storage\`,e);window.removeEventListener(\`hydex-offload-change\`,e)}}`;
}

function fullLocalSubmitGateMatches(source) {
  const pattern=new RegExp(`(${IDENT})=(?:(${IDENT})|\\(0,(${IDENT})\\.(${IDENT})\\))\\((?:"local"===(${IDENT})|(${IDENT})===\x60local\x60)&&${IDENT}(?:\\.${IDENT})?\\)`,"g");
  return [...source.matchAll(pattern)].flatMap(match=>{
    const owner=enclosingFunction(source,match.index,60_000);
    if (!owner) return [];
    const region=owner.region,scopeNames=localScopeNames(region),react=uniqueAlias(region,"useRef");
    const followNames=[...new Set([...region.matchAll(new RegExp(`(${IDENT})\\?\\.type===["\x60]cloud["\x60]`,"g"))].map(m=>m[1]))];
    const submitNames=[...new Set([...region.matchAll(new RegExp(`submitDisabled:(${IDENT})(?=[,}])`,"g"))].map(m=>m[1]))].filter(name=>new RegExp(`${name.replace(/\$/g,"\\$")}=[^;{}]{1,800}\\|\\|${match[1]}[,;]`).test(region));
    if(scopeNames.length!==1 || followNames.length!==1 || submitNames.length!==1 || !react) return [];
    return [{...owner,quota:match,host:match[5]||match[6],scope:scopeNames[0],follow:followNames[0],react}];
  });
}

function matchesHydexFullLocalSubmitGateContract(source) {
  return source.includes(FULL_LOCAL_SUBMIT_MARKER) || fullLocalSubmitGateMatches(source).length === 1;
}

function applyHydexFullLocalSubmitGatePatch(source) {
  if (source.includes(FULL_LOCAL_SUBMIT_MARKER)) return source;
  const matches = fullLocalSubmitGateMatches(source);
  if (matches.length !== 1) {
    warn(`Expected one local composer submit-admission gate, found ${matches.length}`, "Hydex Full local submit admission");
    return source;
  }
  const match = matches[0];
  const local = `hydexFullLocal&&${match.host}===\`local\`&&` +
    `(${match.scope}.value.kind===\`local\`||${match.scope}.value.kind===\`new\`&&${match.scope}.value.cloudThreadPrototype!==!0)&&` +
    `${match.follow}?.type!==\`cloud\``;
  let region = match.region.replace(match.quota[0], `${match.quota[0]}&&!(${local})`);
  region = match.opening +
    `let hydexFullLocal=(0,${match.react}.useSyncExternalStore)(codexLinuxHydexFullLocalSubscribe,codexLinuxHydexFullLocalRead,()=>!1);` +
    region.slice(match.opening.length);
  region = match.opening + fullLocalPreferenceHelpers(FULL_LOCAL_SUBMIT_MARKER) + region.slice(match.opening.length);
  return source.slice(0, match.start) + region + source.slice(match.end);
}

function applyHydexFullLocalCreditGatePatch(source) {
  if (source.includes(FULL_LOCAL_MARKER)) return source;
  const matches = fullLocalCreditGateMatches(source);
  if (matches.length !== 1) {
    warn(`Expected one local usage-admission hook, found ${matches.length}`, "Hydex Full local credit admission");
    return source;
  }
  const match = matches[0];
  const helper = fullLocalPreferenceHelpers(FULL_LOCAL_MARKER);
  const hook = `let hydexFullLocal=(0,${match.react}.useSyncExternalStore)(codexLinuxHydexFullLocalSubscribe,codexLinuxHydexFullLocalRead,()=>!1);`;
  const result = `,hydexFullLocal&&(${match.scope}.value.kind===\`local\`||${match.scope}.value.kind===\`new\`&&${match.scope}.value.cloudThreadPrototype!==!0)&&${match.host}===\`local\`?` +
    `{...${match.result},sendBlocked:!1,isUsageBannerEnabled:${match.result}.imageGenerationLimit!=null,rateLimitWarningThreshold:null}:${match.result}}`;
  const region = match.opening + helper + hook + match.region.slice(match.opening.length, -match.tail.length) + result;
  return source.slice(0, match.start) + region + source.slice(match.end);
}


const descriptors = [
  {
    id: "hydex-desktop-product-name",
    phase: "extracted-app:pre-webview",
    order: 20_690,
    ciPolicy: "optional",
    apply: applyHydexProductNamePatch,
  },
  {
    id: "hydex-offload-request-bridge",
    phase: "webview-asset",
    order: 20700,
    ciPolicy: "optional",
    assetNeedles: ["AppServerRequestClient is missing a message dispatcher"],
    assetMatch: matchesHydexRequestBridgeContract,
    missingDescription: "current app-server request bridge bundle",
    skipDescription: "Hydex offload request bridge patch",
    apply: applyHydexRequestBridgePatch,
  },
  {
    id: "hydex-product-mode-label",
    phase: "webview-asset",
    order: 20705,
    ciPolicy: "optional",
    assetNeedles: ['id:"sidebarElectron.productMode.codex"'],
    assetMatch: matchesHydexProductModeLabelContract,
    missingDescription: "current Codex sidebar product-mode label bundle",
    skipDescription: "Hydex product-mode label patch",
    apply: applyHydexProductModeLabelPatch,
  },
  {
    id: "hydex-product-mode-selected-label",
    phase: "webview-asset",
    order: 20706,
    ciPolicy: "optional",
    assetNeedles: ["h-[1em] w-auto overflow-visible", "data-no-autosize"],
    assetMatch: matchesHydexProductModeSelectedLabelContract,
    missingDescription: "current selected Codex product-mode wordmark bundle",
    skipDescription: "Hydex selected product-mode label patch",
    apply: applyHydexProductModeSelectedLabelPatch,
  },
  {
    id: "hydex-offload-next-turn-updater",
    phase: "extracted-app:pre-webview",
    order: 20709,
    ciPolicy: "optional",
    apply: applyHydexNextTurnUpdaterAssets,
  },
  {
    id: "hydex-offload-composer-control",
    phase: "webview-asset",
    order: 20710,
    ciPolicy: "optional",
    assetNeedles: ["composer.intelligenceDropdown.tooltip", "permissionsHostId:", "selectComposerModelAndReasoningEffort:"],
    assetMatch: matchesHydexComposerContract,
    missingDescription: "current local Codex model picker bundle",
    skipDescription: "Hydex offload composer control patch",
    apply: applyHydexComposerControlPatch,
  },
  {
    id: "hydex-full-local-credit-admission",
    phase: "webview-asset",
    order: 20715,
    ciPolicy: "optional",
    assetNeedles: ["imageGenerationLimit:", "isUsageBannerEnabled:", "rateLimitWarningThreshold:"],
    assetMatch: matchesHydexFullLocalCreditGateContract,
    missingDescription: "current local composer usage-admission hook",
    skipDescription: "Hydex Full local credit admission",
    apply: applyHydexFullLocalCreditGatePatch,
  },
  {
    id: "hydex-full-local-submit-admission",
    phase: "extracted-app:pre-webview",
    order: 20716,
    ciPolicy: "optional",
    apply: applyHydexFullLocalSubmitGateAssets,
  },
  {
    id: "hydex-product-mode-locale-labels",
    phase: "extracted-app:post-webview",
    order: 20720,
    ciPolicy: "optional",
    apply: applyHydexProductModeLocalePatch,
  },
];

module.exports = {
  applyHydexNextTurnUpdaterAssets,
  applyHydexFullLocalSubmitGateAssets,
  applyHydexNextTurnUpdaterPatch,
  matchesHydexNextTurnUpdaterContract,
  FULL_LOCAL_SUBMIT_MARKER,
  applyHydexFullLocalSubmitGatePatch,
  matchesHydexFullLocalSubmitGateContract,
  FULL_LOCAL_MARKER,
  applyHydexFullLocalCreditGatePatch,
  matchesHydexFullLocalCreditGateContract,
  CONTROL_MARKER,
  NEXT_TURN_MARKER,
  PRODUCT_MODE_LOCALE_LABEL_MARKER,
  PRODUCT_MODE_LABEL_MARKER,
  PRODUCT_MODE_SELECTED_LABEL_MARKER,
  REQUEST_MARKER,
  applyHydexProductNamePatch,
  applyHydexComposerControlPatch,
  applyHydexProductModeLocalePatch,
  applyHydexProductModeLabelPatch,
  applyHydexProductModeSelectedLabelPatch,
  applyHydexRequestBridgePatch,
  descriptors,
  matchesHydexComposerContract,
  matchesHydexProductModeLabelContract,
  matchesHydexProductModeSelectedLabelContract,
  matchesHydexRequestBridgeContract,
};
