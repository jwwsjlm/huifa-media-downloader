const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
// Renderer is plain browser code; TypeScript's CommonJS marker is not a browser API.
const renderer = path.join(root, 'dist/renderer.js');
let source = fs.readFileSync(renderer, 'utf8');
source = source.replace(/^"use strict";\r?\n/, '').replace(/^Object\.defineProperty\(exports, "__esModule", \{ value: true \}\);\r?\n/m, '');
fs.writeFileSync(renderer, source);
