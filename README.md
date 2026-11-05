# Otimizador automático da playlist CarTV

- Origem: `playlist-edited (1).m3u` (não é alterada).
- Saída: `playlist-otimizada.m3u` (mesmo caminho e formato usados pelo CarTV).
- Verifica playlists HLS reais, rejeitando páginas HTML e respostas HTTP que não sejam M3U8 válidas.
- Testa três segmentos por playlist de mídia/variante, dentro do limite total de 12 segundos por fonte.
- Após a validação de disponibilidade, usa FFprobe nas duas melhores candidatas aprovadas por canal para conferir resolução efetiva, codec e FPS; quando disponível, considera também o bitrate informado pelo stream.
- Uma falha/inconclusão do FFprobe não reprova a fonte: nesse caso, a avaliação técnica declarada pelo manifesto é mantida.
- Tenta variantes HLS em ordem de pontuação e usa fallback se uma variante falhar.
- Score de seleção: qualidade técnica (50%), confiabilidade histórica (35%) e latência observada (15%). Para as candidatas sondadas, a resolução efetiva substitui a resolução anunciada; bitrate efetivo só é usado quando o stream o informa.
- Agrupa variantes por tvg-id, tvg-name ou nome do canal.
- Mantém histórico por URL (sucessos, falhas, latência e última validação); uma falha isolada mantém temporariamente a última fonte aprovada e duas falhas consecutivas removem o canal da seleção atual. Fontes que voltam a responder podem ser selecionadas novamente automaticamente.
- Cache adaptativo de até 90 minutos somente para fontes com pelo menos quatro aprovações e confiabilidade histórica mínima de 90%; fontes novas ou instáveis continuam sendo testadas.
- Gera `playlist-comparison.md` com canais que melhoraram, pioraram, trocaram de fonte, desapareceram ou são novos. O workflow também disponibiliza o relatório como artefato por 30 dias.
- GitHub Actions executa a cada 30 minutos e publica apenas quando houver mudança.

## URL para o aplicativo

`https://raw.githubusercontent.com/guirossi89-crypto/Playlist-cartv/main/playlist-otimizada.m3u`

## Observações

A validação de disponibilidade não garante reprodução contínua, codec, áudio ou qualidade visual real. O bitrate usado na pontuação é o declarado pelo manifesto HLS, não uma medição contínua do vídeo. Alguns provedores bloqueiam IPs de datacenter, podendo falhar no GitHub Actions mesmo quando funcionam no dispositivo. Se todos os canais falharem em uma execução, a saída e o histórico são preservados por segurança.
