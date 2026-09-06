"use strict";

const fs = require("node:fs");
const path = require("node:path");

const IDENT = "[A-Za-z_$][\\w$]*";
const REQUEST_MARKER = "codexLinuxHydexOffloadRequest";
const CONTROL_MARKER = "codexLinuxHydexOffloadControl";
const NEXT_TURN_MARKER = "codexLinuxHydexOffloadNextTurn";
const HYDEX_PRODUCT_NAME = "Hydex";

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

function warn(message, patchName) {
  console.warn(`WARN: ${message} - skipping ${patchName}`);
}

function requestBridgePattern() {
  return new RegExp(
    `async sendRequest\\((${IDENT}),(${IDENT}),(${IDENT})\\)\\{` +
      `if\\(this\\.dispatchMessage==null\\)throw Error\\(` +
      "`AppServerRequestClient is missing a message dispatcher`" +
      `\\);`,
    "g",
  );
}

function requestBridgeMatches(source) {
  return [...source.matchAll(requestBridgePattern())];
}

function matchesHydexRequestBridgeContract(source) {
  return source.includes(REQUEST_MARKER) || requestBridgeMatches(source).length === 1;
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
    `if((${methodVar}===\`turn/start\`||${methodVar}===\`thread/settings/update\`)&&${paramsVar}!=null){` +
    `let r=null;try{let e=localStorage.getItem(\`hydex.offloadOverride\`);` +
    `r=e===\`force_on\`||e===\`force_off\`?e:null}catch{}` +
    `${paramsVar}=${methodVar}===\`thread/settings/update\`&&${paramsVar}?.threadSettings!=null?` +
    `{...${paramsVar},threadSettings:{...${paramsVar}.threadSettings,modelOffloadOverride:r}}:` +
    `{...${paramsVar},modelOffloadOverride:r}}/*${REQUEST_MARKER}*/`;

  return source.slice(0, match.index + match[0].length) +
    injection +
    source.slice(match.index + match[0].length);
}

function composerSignaturePattern() {
  return new RegExp(
    `function (${IDENT})\\(e\\)\\{let (${IDENT})=\\(0,(${IDENT})\\.c\\)\\(\\d+\\),` +
      `\\{allowAeonDraftModelSelection:(${IDENT}),conversationId:(${IDENT}),` +
      `hideLabel:(${IDENT}),permissionsCwdOverride:(${IDENT}),permissionsHostId:(${IDENT})\\}=e`,
    "g",
  );
}

function composerContractMatches(source) {
  return [...source.matchAll(composerSignaturePattern())].filter((match) => {
    const region = source.slice(match.index, match.index + 30_000);
    return region.includes("data-codex-intelligence-trigger") &&
      region.includes("composer.intelligenceDropdown.tooltip") &&
      region.includes("selectComposerModelAndReasoningEffort") &&
      region.includes("reasoningEffort") &&
      region.includes("persistent");
  });
}

function matchesHydexComposerContract(source) {
  return source.includes(CONTROL_MARKER) || composerContractMatches(source).length === 1;
}

function uniqueAlias(region, method) {
  const matches = [
    ...region.matchAll(new RegExp(`\\(0,(${IDENT})\\.${method}\\)\\(`, "g")),
  ];
  const aliases = [...new Set(matches.map((match) => match[1]))];
  return aliases.length === 1 ? aliases[0] : null;
}

function applyHydexComposerControlPatch(source) {
  if (source.includes(CONTROL_MARKER) && source.includes(NEXT_TURN_MARKER)) return source;

  const matches = composerContractMatches(source);
  if (matches.length !== 1) {
    warn(
      `Expected one local Codex model picker, found ${matches.length}`,
      "Hydex offload composer control patch",
    );
    return source;
  }

  const [signature] = matches;
  const functionStart = signature.index;
  const regionLimit = source.slice(functionStart, functionStart + 30_000);
  const suffixPattern = new RegExp(
    `\\}function ${IDENT}\\(e\\)\\{let\\{reasoningEffort:${IDENT}\\}=e;` +
      `return ${IDENT}===\\\`persistent\\\`\\}`,
  );
  const suffixMatch = regionLimit.match(suffixPattern);
  if (suffixMatch == null || suffixMatch.index == null) {
    warn("Could not bound the local model picker", "Hydex offload composer control patch");
    return source;
  }

  const functionEnd = functionStart + suffixMatch.index + 1;
  const region = source.slice(functionStart, functionEnd);
  const reactAlias = uniqueAlias(region, "useState");
  const jsxAliases = [
    ...region.matchAll(new RegExp(`\\(0,(${IDENT})\\.(?:jsx|jsxs)\\)\\(`, "g")),
  ].map((match) => match[1]);
  const uniqueJsxAliases = [...new Set(jsxAliases)];
  const jsxAlias = uniqueJsxAliases.length === 1 ? uniqueJsxAliases[0] : null;
  const modelHookPattern = new RegExp(
    `\\{modelSettings:(${IDENT}),setDefaultModelAndReasoningEffort:(${IDENT}),` +
      `setModelAndReasoningEffort:(${IDENT}),` +
      `setModelAndReasoningEffortForNextTurn:(${IDENT})\\}=`,
    "g",
  );
  const modelHookMatches = [...source.matchAll(modelHookPattern)];
  const selectionPattern = new RegExp(
    `let (${IDENT})=${IDENT}\\(${IDENT}\\),\\{modelSettings:(${IDENT}),` +
      `selectComposerModelAndReasoningEffort:${IDENT},` +
      `setDefaultModelAndReasoningEffort:${IDENT},` +
      `setModelAndReasoningEffort:${IDENT}\\}=\\1,`,
  );
  const selectionMatch = region.match(selectionPattern);
  const resultMatches = [
    ...region.matchAll(new RegExp(`let (${IDENT});return`, "g")),
  ];

  if (
    reactAlias == null ||
    jsxAlias == null ||
    modelHookMatches.length !== 1 ||
    selectionMatch == null ||
    resultMatches.length !== 1
  ) {
    warn("Could not resolve local model picker symbols", "Hydex offload composer control patch");
    return source;
  }

  const [modelHookMatch] = modelHookMatches;
  const modelHookStart = modelHookMatch.index;
  const modelSettingsHookVar = modelHookMatch[1];
  const defaultModelSetterVar = modelHookMatch[2];
  const modelSetterVar = modelHookMatch[3];
  const nextTurnSetterVar = modelHookMatch[4];
  const passthroughPattern = new RegExp(
    `\\{modelSettings:${modelSettingsHookVar},` +
      `setDefaultModelAndReasoningEffort:${defaultModelSetterVar},` +
      `setModelAndReasoningEffort:${modelSetterVar},` +
      `selectComposerModelAndReasoningEffort:(${IDENT})\\}`,
    "g",
  );
  const passthroughSearch = source.slice(modelHookStart, functionStart);
  const passthroughMatches = [...passthroughSearch.matchAll(passthroughPattern)];
  if (passthroughMatches.length !== 1) {
    warn("Could not expose the next-turn model updater", "Hydex offload composer control patch");
    return source;
  }

  const passthroughMatch = passthroughMatches[0];
  const passthroughStart = modelHookStart + passthroughMatch.index;
  const passthrough = passthroughMatch[0];
  const patchedPassthrough =
    `${passthrough.slice(0, -1)},/*${NEXT_TURN_MARKER}*/` +
    `setModelAndReasoningEffortForNextTurn:${nextTurnSetterVar}}`;
  const conversationVar = signature[5];
  const modelSelectionVar = selectionMatch[1];
  const modelSettingsVar = selectionMatch[2];
  const resultVar = resultMatches[0][1];
  const originalTail = `,${resultVar}}`;
  if (!region.endsWith(originalTail)) {
    warn("Could not resolve local model picker return", "Hydex offload composer control patch");
    return source;
  }

  const helper =
    `/*${CONTROL_MARKER}*/` +
    `function codexLinuxHydexOffloadRead(){try{let e=localStorage.getItem(\`hydex.offloadOverride\`);` +
    `return e===\`force_on\`||e===\`force_off\`?e:\`auto\`}catch{return\`auto\`}}` +
    `function ${CONTROL_MARKER}({onApply:e}){let[t,n]=(0,${reactAlias}.useState)(codexLinuxHydexOffloadRead),` +
    `[r,i]=(0,${reactAlias}.useState)(!1),a=(0,${reactAlias}.useRef)(null),` +
    `o=(0,${reactAlias}.useRef)(null),s=t===\`force_on\`?\`Hydex on\`:t===\`force_off\`?\`Hydex off\`:\`Hydex auto\`,` +
    `c=c=>{let l=c.currentTarget.dataset.value;if(l!==\`auto\`&&l!==\`force_on\`&&l!==\`force_off\`)return;` +
    `n(l);try{l===\`auto\`?localStorage.removeItem(\`hydex.offloadOverride\`):` +
    `localStorage.setItem(\`hydex.offloadOverride\`,l)}catch{}let u=l===\`auto\`?null:l;` +
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
    `return(0,${jsxAlias}.jsxs)(\`span\`,{className:\`relative inline-flex\`,children:[` +
    `(0,${jsxAlias}.jsxs)(\`button\`,{ref:a,type:\`button\`,\"aria-haspopup\":\`menu\`,\"aria-expanded\":r,` +
    `\"aria-label\":\`Hydex offload\`,title:\`Hydex offload\`,onClick:l,` +
    `className:\`h-token-button-composer flex max-w-[108px] shrink-0 cursor-interaction items-center ` +
    `justify-between gap-1 rounded-lg border border-default bg-primary px-2 text-sm text-default ` +
    `outline-none hover:bg-primary-ghost-hover focus-visible:ring-2 focus-visible:ring-ring\`,` +
    `children:[(0,${jsxAlias}.jsx)(\`span\`,{className:\`truncate\`,children:s}),` +
    `(0,${jsxAlias}.jsx)(\`span\`,{\"aria-hidden\":!0,className:\`text-tertiary\`,children:\`⌄\`})]}),` +
    `(0,${jsxAlias}.jsxs)(\`div\`,{ref:o,popover:\`auto\`,role:\`menu\`,\"aria-label\":\`Hydex offload\`,` +
    `onKeyDown:u,onToggle:d,className:\`min-w-40 rounded-xl bg-surface-elevated-secondary p-1 ` +
    `text-default shadow-lg ring-1 ring-border\`,style:{position:\`fixed\`,inset:\`auto\`,margin:0},children:[` +
    `(0,${jsxAlias}.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`auto\`,` +
    `\"data-value\":\`auto\`,onClick:c,className:f+(t===\`auto\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex auto\`,t===\`auto\`?(0,${jsxAlias}.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]}),` +
    `(0,${jsxAlias}.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`force_on\`,` +
    `\"data-value\":\`force_on\`,onClick:c,className:f+(t===\`force_on\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex on\`,t===\`force_on\`?(0,${jsxAlias}.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]}),` +
    `(0,${jsxAlias}.jsxs)(\`button\`,{type:\`button\`,role:\`menuitemradio\`,\"aria-checked\":t===\`force_off\`,` +
    `\"data-value\":\`force_off\`,onClick:c,className:f+(t===\`force_off\`?\` bg-primary-ghost-hover\`:\`\`),` +
    `children:[\`Hydex off\`,t===\`force_off\`?(0,${jsxAlias}.jsx)(\`span\`,{\"aria-hidden\":!0,children:\`✓\`}):null]})]})]})}`;
  const onApply =
    `${conversationVar}==null?void 0:e=>${modelSelectionVar}.setModelAndReasoningEffortForNextTurn(` +
    `${modelSettingsVar}.model,${modelSettingsVar}.reasoningEffort,` +
    `{threadSettings:{modelOffloadOverride:e}})`;
  const replacementTail =
    `,(0,${jsxAlias}.jsxs)(\`span\`,{className:\`inline-flex min-w-0 items-center gap-1\`,` +
    `children:[${resultVar},(0,${jsxAlias}.jsx)(${CONTROL_MARKER},{onApply:${onApply}})]})}`;
  const patchedRegion = region.slice(0, -originalTail.length) + replacementTail;
  const patchedPrefix = source.slice(0, passthroughStart) +
    patchedPassthrough +
    source.slice(passthroughStart + passthrough.length, functionStart);

  return patchedPrefix + helper + patchedRegion + source.slice(functionEnd);
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
    pattern: /^app-initial-[^.]+\.js$/,
    assetMatch: matchesHydexRequestBridgeContract,
    missingDescription: "current app-server request bridge bundle",
    skipDescription: "Hydex offload request bridge patch",
    apply: applyHydexRequestBridgePatch,
  },
  {
    id: "hydex-offload-composer-control",
    phase: "webview-asset",
    order: 20710,
    ciPolicy: "optional",
    pattern: /^app-primary-[^.]+\.js$/,
    assetMatch: matchesHydexComposerContract,
    missingDescription: "current local Codex model picker bundle",
    skipDescription: "Hydex offload composer control patch",
    apply: applyHydexComposerControlPatch,
  },
];

module.exports = {
  CONTROL_MARKER,
  NEXT_TURN_MARKER,
  REQUEST_MARKER,
  applyHydexProductNamePatch,
  applyHydexComposerControlPatch,
  applyHydexRequestBridgePatch,
  descriptors,
  matchesHydexComposerContract,
  matchesHydexRequestBridgeContract,
};
