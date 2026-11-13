#!/usr/bin/env node
"use strict";
const transport = require("./cache/chat_api_cache/native_mcp.cjs");
function serve(options = {}) { return transport.serve({ runtime: __dirname, ...options }); }
if (require.main === module) serve();
module.exports = { serve };
