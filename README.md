# CarTV — Otimizador e monitor de saúde da playlist IPTV

Projeto que verifica fontes IPTV, compara alternativas do mesmo canal e publica uma playlist otimizada para uso no aplicativo CarTV. O critério central é **reprodução estável e fluida**: resolução alta, sozinha, não torna uma fonte melhor.

## Como funciona

1. Lê a playlist de origem `playlist-edited (1).m3u`.
2. Agrupa fontes que representam o mesmo canal, normalizando nomes e sufixos de qualidade sem misturar canais regionais diferentes.
3. Testa a disponibilidade das fontes e seleciona as melhores candidatas por canal.
4. Atualiza o histórico de saúde por URL e compara o resultado com a playlist publicada anteriormente.
5. Gera `playlist-otimizada.m3u`, o relatório comparativo e o estado histórico.

A lista de origem é preservada: o processo automático não a sobrescreve.

## Critérios de validação e seleção

- **HLS:** verifica se a resposta é uma playlist M3U8 válida, rejeita páginas HTML e testa até três segmentos da mídia.
- **MPEG-TS:** quando a URL termina em `.ts`, verifica os pacotes de transporte em vez de exigir um manifesto HLS.
- **Limite por fonte:** orçamento de até 12 segundos para a verificação rápida.
- **Variantes HLS:** testa até três variantes reproduzíveis e não escolhe automaticamente a de maior resolução.
- **FFprobe:** examina até duas candidatas aprovadas por canal para obter características reais do vídeo, como resolução, codec e FPS. Se o FFprobe falhar ou não conseguir concluir a análise, isso não reprova por si só uma fonte que passou na validação de disponibilidade.
- **Ranking:** prioriza estabilidade contínua comprovada, confiabilidade histórica e latência. A resolução tem peso residual; não deve sacrificar fluidez.
- **Histórico:** registra sucessos, falhas, latência, última validação e, quando disponível, dados técnicos e resultados dos testes prolongados.
- **Cache adaptativo:** pode reaproveitar por até 90 minutos a última validação de fontes com pelo menos quatro sucessos e confiabilidade histórica mínima de 90%. Fontes novas ou instáveis continuam sendo verificadas.
- **Fontes preferenciais:** preferências explícitas definidas no projeto prevalecem sobre o ranking automático, mas a fonte continua sendo monitorada.

## Testes de estabilidade contínua

Além da checagem rápida, o projeto executa testes prolongados em rodízio:

- Duração-alvo de 120 segundos por fonte selecionada.
- Até 12 fontes por execução, com até quatro testes simultâneos.
- A seleção considera fontes publicadas e candidatas, permitindo acompanhar a estabilidade ao longo do tempo.
- O teste baixa segmentos HLS em sequência e avalia pontualidade dos downloads, regularidade, falhas e margem de velocidade.
- Uma amostra só é considerada válida quando cumpre os requisitos mínimos de duração e quantidade de segmentos. O teste não decodifica o vídeo e não substitui a reprodução real no CarTV.

## Proteção da identidade dos canais

O projeto evita que uma falha isolada de verificação apague imediatamente um canal da playlist:

- Mantém a fonte anteriormente publicada quando ela ainda está disponível na lista de origem e não há uma candidata aprovada que a substitua.
- Quando necessário, pode recuperar uma fonte com histórico positivo.
- Para canais do grupo **Documentários**, preserva a identidade do canal mesmo quando não há fonte atualmente validada, usando a melhor candidata disponível para permitir reavaliação posterior.
- Fontes mantidas sem validação atual são identificadas no estado e no relatório; sua presença na playlist **não significa que estejam funcionando naquele momento**.
- Quando uma fonte volta a responder, ela pode voltar a participar normalmente da seleção automática.

Essa proteção reduz desaparecimentos indevidos, mas não garante que toda transmissão esteja ativa ou reproduza sem interrupções.

## Agendamento e publicação

O GitHub Actions executa automaticamente **uma vez por hora**, no minuto 17 (cron UTC: `17 * * * *`). O GitHub pode atrasar o início de execuções agendadas em períodos de alta demanda.

Também há execução manual por `workflow_dispatch` e validação em pull requests que alterem scripts ou o próprio workflow.

Quando há mudanças, o workflow publica a playlist otimizada, o histórico e o relatório no branch principal. A publicação tenta reconciliar alterações concorrentes antes de enviar o resultado. Se nenhuma mudança for detectada, não cria um commit desnecessário.

O relatório de cada execução também é disponibilizado como artefato do GitHub Actions por 30 dias.

## Arquivos principais

| Arquivo | Função |
|---|---|
| `playlist-edited (1).m3u` | Lista de origem; não é alterada pelo otimizador |
| `playlist-otimizada.m3u` | Playlist gerada e publicada para o CarTV |
| `playlist-health-state.json` | Histórico de saúde e estabilidade por fonte |
| `playlist-comparison.md` | Comparação entre execuções |
| `scripts/optimize_playlist.py` | Verificação, ranking, proteção de canais e geração da playlist |
| `scripts/continuous_stability.py` | Teste prolongado de estabilidade HLS |
| `.github/workflows/playlist-check.yml` | Agendamento, validação e publicação automática |

## URL da playlist para o CarTV

Use este endereço no aplicativo. Ele permanece estável mesmo quando o conteúdo da playlist é atualizado:

```text
https://raw.githubusercontent.com/guirossi89-crypto/Playlist-cartv/main/playlist-otimizada.m3u
```

## EPG

A integração XMLTV/EPG está desativada. O workflow não baixa guia de programação, não associa IDs XMLTV e remove referências `x-tvg-url` e `url-tvg` do cabeçalho da saída.

## Limitações importantes

- Uma resposta HTTP válida ou alguns segmentos baixados não garantem reprodução contínua no player.
- O teste de estabilidade mede a entrega dos segmentos, mas não reproduz nem decodifica integralmente o vídeo como o CarTV.
- O FFprobe fornece metadados do fluxo acessível; não mede sozinho a fluidez percebida durante uma sessão longa.
- Alguns provedores bloqueiam endereços IP de datacenter. Uma fonte pode falhar no GitHub Actions e funcionar no dispositivo, ou o contrário.
- Canais preservados por proteção de identidade podem permanecer listados mesmo sem validação atual. Consulte o relatório e o estado de saúde antes de considerá-los operacionais.
- Se nenhuma fonte for aprovada em uma execução, o processo preserva a playlist publicada e o histórico existentes por segurança.

## Objetivo do projeto

Manter uma playlist útil no CarTV, priorizando canais que **abram e reproduzam sem travamentos**, sem penalizar fontes estáveis apenas por oferecerem resolução menor. A seleção automática deve melhorar a experiência sem provocar exclusões precipitadas de canais.
