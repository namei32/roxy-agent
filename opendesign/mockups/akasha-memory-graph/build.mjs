import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { readFile, writeFile } from 'node:fs/promises';

const artifact = dirname(fileURLToPath(import.meta.url));
const index = process.argv.indexOf('--dependencies');
const dependencies = index >= 0 ? resolve(process.argv[index+1]) : resolve(artifact, '../../../node_modules');
const require = createRequire(join(dependencies, '../package.json'));
const { build } = require('esbuild');
const result = await build({ entryPoints: [join(artifact, 'src/App.jsx')], outfile: join(artifact, 'app.js'), bundle: true, format: 'esm', jsx: 'automatic', minify: true, target: ['es2022'], platform: 'browser', nodePaths: [dependencies], define: { 'process.env.NODE_ENV': '"production"' }, legalComments: 'linked', metafile: true, logLevel: 'info' });
const versions = {};
for (const name of ['react', 'react-dom', 'lucide-react', 'esbuild']) versions[name] = JSON.parse(await readFile(join(dependencies, name, 'package.json'), 'utf8')).version;
await writeFile(join(artifact, 'build-info.json'), JSON.stringify({ artifact: 'Akasha memory graph prototype', dependencies: versions, networkRuntimeDependencies: [], outputBytes: Object.values(result.metafile.outputs).reduce((sum,item)=>sum+item.bytes,0), fixtureOnly: true }, null, 2)+'\n');
