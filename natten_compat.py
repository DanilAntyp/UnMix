"""Pure-torch reimplementation of the legacy NATTEN functional API
(natten1dqkrpb / natten1dav / natten2dqkrpb / natten2dav) that allin1's
DiNAT layers expect. New natten (>=0.20) removed these; this shim restores
them with plain gather/einsum ops. Slower than the fused kernels but fine
for one-off analysis, and it lets the pretrained model run unmodified.

Semantics follow NATTEN: each query attends to exactly `kernel` neighbors;
windows are centered where possible and clamp-shifted at the edges; with
dilation d, attention runs independently over each of the d phase
subsequences. rpb is indexed by the true relative offset (in dilation units).
"""
import torch


def _win(L, K, device):
    idx = torch.arange(L, device=device)
    starts = (idx - (K - 1) // 2).clamp(0, max(L - K, 0))
    win = starts[:, None] + torch.arange(K, device=device)[None, :]
    win = win.clamp(0, L - 1)
    rel = win - idx[:, None] + (K - 1)          # 0 .. 2K-2
    return win, rel


def _qkrpb1(q, k, rpb, K):
    B, H, L, D = q.shape
    win, rel = _win(L, K, q.device)
    kw = k[:, :, win]                            # [B,H,L,K,D]
    attn = torch.einsum('bhld,bhlkd->bhlk', q, kw)
    if rpb is not None:
        attn = attn + rpb[:, rel][None]          # [1,H,L,K]
    return attn


def _av1(attn, v, K):
    B, H, L, _ = attn.shape
    win, _ = _win(L, K, attn.device)
    vw = v[:, :, win]
    return torch.einsum('bhlk,bhlkd->bhld', attn, vw)


def natten1dqkrpb(q, k, rpb, kernel_size, dilation=1):
    if dilation <= 1:
        return _qkrpb1(q, k, rpb, kernel_size)
    L = q.shape[2]
    out = q.new_zeros(q.shape[0], q.shape[1], L, kernel_size)
    for p in range(dilation):
        out[:, :, p::dilation] = _qkrpb1(q[:, :, p::dilation], k[:, :, p::dilation],
                                         rpb, kernel_size)
    return out


def natten1dav(attn, v, kernel_size, dilation=1):
    if dilation <= 1:
        return _av1(attn, v, kernel_size)
    out = torch.empty_like(v)
    for p in range(dilation):
        out[:, :, p::dilation] = _av1(attn[:, :, p::dilation], v[:, :, p::dilation],
                                      kernel_size)
    return out


def _qkrpb2(q, k, rpb, K):
    B, Hh, Hs, Ws, D = q.shape
    winH, relH = _win(Hs, K, q.device)
    winW, relW = _win(Ws, K, q.device)
    kw = k[:, :, winH]                           # [B,H,Hs,K,Ws,D]
    kw = kw[:, :, :, :, winW]                    # [B,H,Hs,K,Ws,K,D]
    kw = kw.permute(0, 1, 2, 4, 3, 5, 6)         # [B,H,Hs,Ws,K,K,D]
    attn = torch.einsum('bhxyd,bhxyklD->bhxykl'.replace('D', 'd'), q, kw)
    if rpb is not None:
        bias = rpb[:, relH][:, :, :, None, None] if False else None
        # rpb: [H, 2K-1, 2K-1]; bias[h, x, y, kh, kw] = rpb[h, relH[x,kh], relW[y,kw]]
        bias = rpb[:, relH[:, :, None, None], relW[None, None, :, :]]  # [H,Hs,K,Ws,K]
        bias = bias.permute(0, 1, 3, 2, 4)                              # [H,Hs,Ws,K,K]
        attn = attn + bias[None]
    return attn.reshape(B, Hh, Hs, Ws, K * K)


def _av2(attn, v, K):
    B, Hh, Hs, Ws, KK = attn.shape
    attn = attn.reshape(B, Hh, Hs, Ws, K, K)
    winH, _ = _win(Hs, K, attn.device)
    winW, _ = _win(Ws, K, attn.device)
    vw = v[:, :, winH][:, :, :, :, winW].permute(0, 1, 2, 4, 3, 5, 6)  # [B,H,Hs,Ws,K,K,D]
    return torch.einsum('bhxykl,bhxykld->bhxyd', attn, vw)


def natten2dqkrpb(q, k, rpb, kernel_size, dilation=1):
    if dilation <= 1:
        return _qkrpb2(q, k, rpb, kernel_size)
    B, Hh, Hs, Ws, D = q.shape
    out = q.new_zeros(B, Hh, Hs, Ws, kernel_size * kernel_size)
    for ph in range(dilation):
        for pw in range(dilation):
            out[:, :, ph::dilation, pw::dilation] = _qkrpb2(
                q[:, :, ph::dilation, pw::dilation],
                k[:, :, ph::dilation, pw::dilation], rpb, kernel_size)
    return out


def natten2dav(attn, v, kernel_size, dilation=1):
    if dilation <= 1:
        return _av2(attn, v, kernel_size)
    out = torch.empty_like(v)
    for ph in range(dilation):
        for pw in range(dilation):
            out[:, :, ph::dilation, pw::dilation] = _av2(
                attn[:, :, ph::dilation, pw::dilation],
                v[:, :, ph::dilation, pw::dilation], kernel_size)
    return out


def install():
    """Inject the legacy names into natten.functional before allin1 imports them."""
    import natten.functional as nf
    nf.natten1dqkrpb = natten1dqkrpb
    nf.natten1dav = natten1dav
    nf.natten2dqkrpb = natten2dqkrpb
    nf.natten2dav = natten2dav
