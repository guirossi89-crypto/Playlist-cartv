# Otimizador automático da playlist CarTV

- Origem: `playlist-edited (1).m3u` (não é alterada).
- Saída: `playlist-otimizada.m3u` (mesmo caminho e formato usados pelo CarTV).
- Verifica playlists HLS reais, rejeitando páginas HTML e respostas HTTP que não sejam M3U8 válidas.
- Testa até dois segmentos por variante, dentro do limite total de 12 segundos por fonte.
- Tenta variantes HLS em ordem de pontuação e usa fallback se uma variante falhar.
- Pontuação considera resolução, bitrate declarado no manifesto e latência observada.
- Agrupa variantes por tvg-id, tvg-name ou nome do canal.
- Uma falha isolada mantém temporariamente a última fonte aprovada; duas falhas consecutivas removem o canal.
- GitHub Actions executa a cada 30 minutos e publica apenas quando houver mudança.

## URL para o aplicativo

`https://raw.githubusercontent.com/guirossi89-crypto/Playlist-cartv/main/playlist-otimizada.m3u`

## Observações

A validação de disponibilidade não garante reprodução contínua, codec, áudio ou qualidade visual real. O bitrate usado na pontuação é o declarado pelo manifesto HLS, não uma medição contínua do vídeo. Alguns provedores bloqueiam IPs de datacenter, podendo falhar no GitHub Actions mesmo quando funcionam no dispositivo. Se todos os canais falharem em uma execução, a saída e o histórico são preservados por segurança.
