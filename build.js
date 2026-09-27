// build.js
// Gera os HTMLs de producao a partir dos templates _base.html,
// substituindo placeholders pelas chaves reais (Environment Variables).

const fs = require('fs');

const substituicoes = [
    { placeholder: /CHAVE_MAPBOX_AQUI/g,   env: 'MAPBOX_KEY' },
    { placeholder: /URL_SUPABASE_AQUI/g,   env: 'SUPABASE_URL' },
    { placeholder: /CHAVE_SUPABASE_AQUI/g, env: 'SUPABASE_ANON_KEY' },
];

const arquivos = [
    { fonte: 'index_base.html',     saida: 'index.html' },
    { fonte: 'moderacao_base.html', saida: 'moderacao.html' },
];

// Verifica se todas as variaveis de ambiente estao definidas
const faltando = substituicoes
    .filter(s => !process.env[s.env])
    .map(s => s.env);

if (faltando.length > 0) {
    console.error('ERRO: Variaveis de ambiente ausentes:');
    faltando.forEach(v => console.error(`  - ${v}`));
    process.exit(1);
}

// Processa cada arquivo
for (const { fonte, saida } of arquivos) {
    if (!fs.existsSync(fonte)) {
        console.warn(`AVISO: ${fonte} nao encontrado. Pulando.`);
        continue;
    }

    let html = fs.readFileSync(fonte, 'utf8');

    for (const { placeholder, env } of substituicoes) {
        html = html.replace(placeholder, process.env[env]);
    }

    fs.writeFileSync(saida, html);
    console.log(`Gerado: ${saida}`);
}

console.log('Build concluido com sucesso.');