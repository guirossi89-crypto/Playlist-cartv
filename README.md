# Otimizador automático da playlist CarTV

- Origem: `playlist-edited (1).m3u` (não é alterada).
- Saída: `playlist-otimizada.m3u`.
- Verifica HTTP/HLS e até dois segmentos por fonte.
- Agrupa variantes por tvg-id, tvg-name ou nome do canal.
- Critério `best`: maior resolução identificada; em empate, menor latência.
- Preserva as diretivas EXTINF e EXTVLCOPT da fonte escolhida.
- GitHub Actions executa a cada 30 minutos e publica apenas quando houver mudança.

## URL para o aplicativo
`https://raw.githubusercontent.com/guirossi89-crypto/Playlist-cartv/refs/heads/main/playlist-otimizada.m3u`

## Observações
A validação de disponibilidade não garante reprodução contínua, codec ou áudio. Alguns provedores bloqueiam IPs de datacenter, podendo falhar no GitHub Actions mesmo quando funcionam no dispositivo. Se nenhuma variante de um canal responder, esse canal não entra na saída; a playlist original continua intacta e será reavaliada no próximo ciclo.
