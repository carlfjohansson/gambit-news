#!/usr/bin/env python3
"""Console chess with full standard rules.

Legal move generation (including castling, en passant, promotion),
check/checkmate/stalemate detection, draw detection (50-move rule,
threefold repetition, insufficient material), and an optional minimax
AI opponent.

Run: python3 chess_game.py
Moves are entered in coordinate form, e.g. e2e4, e7e8q. Castling: O-O / O-O-O.
"""

import random
import sys

FILES = "abcdefgh"

PIECE_VALUES = {"P": 100, "N": 320, "B": 330, "R": 500, "Q": 900, "K": 20000}

UNICODE = {
    ("w", "P"): "♙", ("w", "N"): "♘", ("w", "B"): "♗",
    ("w", "R"): "♖", ("w", "Q"): "♕", ("w", "K"): "♔",
    ("b", "P"): "♟", ("b", "N"): "♞", ("b", "B"): "♝",
    ("b", "R"): "♜", ("b", "Q"): "♛", ("b", "K"): "♚",
}

KNIGHT_OFFSETS = [(1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)]
KING_OFFSETS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
BISHOP_DIRS = [(1, 1), (1, -1), (-1, 1), (-1, -1)]
ROOK_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def in_bounds(r, f):
    return 0 <= r < 8 and 0 <= f < 8


class Move:
    __slots__ = ("fr", "to", "promo", "is_ep", "castle")

    def __init__(self, fr, to, promo=None, is_ep=False, castle=None):
        self.fr = fr
        self.to = to
        self.promo = promo
        self.is_ep = is_ep
        self.castle = castle

    def uci(self):
        r1, f1 = self.fr
        r2, f2 = self.to
        s = f"{FILES[f1]}{r1 + 1}{FILES[f2]}{r2 + 1}"
        if self.promo:
            s += self.promo.lower()
        return s


class ChessGame:
    def __init__(self):
        self.board = self._start_board()
        self.turn = "w"
        self.castling = {"wK": True, "wQ": True, "bK": True, "bQ": True}
        self.ep_target = None
        self.halfmove_clock = 0
        self.fullmove = 1
        self.history = []
        self.log = []

    @staticmethod
    def _start_board():
        board = [[None] * 8 for _ in range(8)]
        back = ["R", "N", "B", "Q", "K", "B", "N", "R"]
        for f in range(8):
            board[0][f] = ("w", back[f])
            board[1][f] = ("w", "P")
            board[6][f] = ("b", "P")
            board[7][f] = ("b", back[f])
        return board

    # ---------- lightweight clone used during search/legality checks ----------
    def copy(self):
        g = ChessGame.__new__(ChessGame)
        g.board = [row[:] for row in self.board]
        g.turn = self.turn
        g.castling = dict(self.castling)
        g.ep_target = self.ep_target
        g.halfmove_clock = self.halfmove_clock
        g.fullmove = self.fullmove
        g.history = self.history
        g.log = self.log
        return g

    # ---------- queries ----------
    def king_square(self, color):
        for r in range(8):
            for f in range(8):
                if self.board[r][f] == (color, "K"):
                    return (r, f)
        return None

    def is_attacked(self, r, f, by_color):
        board = self.board
        direction = 1 if by_color == "w" else -1
        for df in (-1, 1):
            rr, ff = r - direction, f + df
            if in_bounds(rr, ff) and board[rr][ff] == (by_color, "P"):
                return True
        for dr, df in KNIGHT_OFFSETS:
            rr, ff = r + dr, f + df
            if in_bounds(rr, ff) and board[rr][ff] == (by_color, "N"):
                return True
        for dr, df in KING_OFFSETS:
            rr, ff = r + dr, f + df
            if in_bounds(rr, ff) and board[rr][ff] == (by_color, "K"):
                return True
        for dr, df in BISHOP_DIRS:
            rr, ff = r + dr, f + df
            while in_bounds(rr, ff):
                p = board[rr][ff]
                if p is not None:
                    if p[0] == by_color and p[1] in ("B", "Q"):
                        return True
                    break
                rr += dr
                ff += df
        for dr, df in ROOK_DIRS:
            rr, ff = r + dr, f + df
            while in_bounds(rr, ff):
                p = board[rr][ff]
                if p is not None:
                    if p[0] == by_color and p[1] in ("R", "Q"):
                        return True
                    break
                rr += dr
                ff += df
        return False

    def in_check(self, color):
        ks = self.king_square(color)
        return self.is_attacked(ks[0], ks[1], "b" if color == "w" else "w")

    # ---------- move generation ----------
    def pseudo_moves(self, color):
        moves = []
        board = self.board
        for r in range(8):
            for f in range(8):
                p = board[r][f]
                if p is None or p[0] != color:
                    continue
                kind = p[1]
                if kind == "P":
                    moves.extend(self._pawn_moves(r, f, color))
                elif kind == "N":
                    for dr, df in KNIGHT_OFFSETS:
                        rr, ff = r + dr, f + df
                        if in_bounds(rr, ff) and (board[rr][ff] is None or board[rr][ff][0] != color):
                            moves.append(Move((r, f), (rr, ff)))
                elif kind == "K":
                    for dr, df in KING_OFFSETS:
                        rr, ff = r + dr, f + df
                        if in_bounds(rr, ff) and (board[rr][ff] is None or board[rr][ff][0] != color):
                            moves.append(Move((r, f), (rr, ff)))
                    moves.extend(self._castle_moves(color))
                elif kind in ("B", "R", "Q"):
                    dirs = []
                    if kind in ("B", "Q"):
                        dirs += BISHOP_DIRS
                    if kind in ("R", "Q"):
                        dirs += ROOK_DIRS
                    for dr, df in dirs:
                        rr, ff = r + dr, f + df
                        while in_bounds(rr, ff):
                            target = board[rr][ff]
                            if target is None:
                                moves.append(Move((r, f), (rr, ff)))
                            else:
                                if target[0] != color:
                                    moves.append(Move((r, f), (rr, ff)))
                                break
                            rr += dr
                            ff += df
        return moves

    def _pawn_moves(self, r, f, color):
        moves = []
        board = self.board
        direction = 1 if color == "w" else -1
        start_rank = 1 if color == "w" else 6
        promo_rank = 7 if color == "w" else 0
        rr = r + direction
        if in_bounds(rr, f) and board[rr][f] is None:
            self._add_pawn_move(moves, (r, f), (rr, f), promo_rank)
            rr2 = r + 2 * direction
            if r == start_rank and board[rr2][f] is None:
                moves.append(Move((r, f), (rr2, f)))
        for df in (-1, 1):
            ff = f + df
            if not in_bounds(rr, ff):
                continue
            target = board[rr][ff]
            if target is not None and target[0] != color:
                self._add_pawn_move(moves, (r, f), (rr, ff), promo_rank)
            elif self.ep_target == (rr, ff):
                moves.append(Move((r, f), (rr, ff), is_ep=True))
        return moves

    @staticmethod
    def _add_pawn_move(moves, fr, to, promo_rank):
        if to[0] == promo_rank:
            for promo in ("Q", "R", "B", "N"):
                moves.append(Move(fr, to, promo=promo))
        else:
            moves.append(Move(fr, to))

    def _castle_moves(self, color):
        moves = []
        rank = 0 if color == "w" else 7
        if self.in_check(color):
            return moves
        opp = "b" if color == "w" else "w"
        board = self.board
        if self.castling[color + "K"]:
            if board[rank][5] is None and board[rank][6] is None and board[rank][7] == (color, "R"):
                if not self.is_attacked(rank, 5, opp) and not self.is_attacked(rank, 6, opp):
                    moves.append(Move((rank, 4), (rank, 6), castle="K"))
        if self.castling[color + "Q"]:
            if (board[rank][1] is None and board[rank][2] is None and board[rank][3] is None
                    and board[rank][0] == (color, "R")):
                if not self.is_attacked(rank, 3, opp) and not self.is_attacked(rank, 2, opp):
                    moves.append(Move((rank, 4), (rank, 2), castle="Q"))
        return moves

    def legal_moves(self, color=None):
        color = color or self.turn
        legal = []
        for m in self.pseudo_moves(color):
            g = self.copy()
            g._apply(m)
            if not g.in_check(color):
                legal.append(m)
        return legal

    # ---------- apply ----------
    def _apply(self, move):
        board = self.board
        r1, f1 = move.fr
        r2, f2 = move.to
        piece = board[r1][f1]
        color, kind = piece
        captured = board[r2][f2]

        self.ep_target = None
        if kind == "P" or captured is not None or move.is_ep:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        if move.is_ep:
            board[r1][f2] = None

        board[r2][f2] = piece
        board[r1][f1] = None

        if move.promo:
            board[r2][f2] = (color, move.promo)

        if kind == "P" and abs(r2 - r1) == 2:
            self.ep_target = ((r1 + r2) // 2, f1)

        if move.castle == "K":
            board[r1][5] = board[r1][7]
            board[r1][7] = None
        elif move.castle == "Q":
            board[r1][3] = board[r1][0]
            board[r1][0] = None

        if kind == "K":
            self.castling[color + "K"] = False
            self.castling[color + "Q"] = False
        if kind == "R":
            if f1 == 0:
                self.castling[color + "Q"] = False
            elif f1 == 7:
                self.castling[color + "K"] = False
        if captured is not None and captured[1] == "R":
            if (r2, f2) == (0, 0):
                self.castling["wQ"] = False
            elif (r2, f2) == (0, 7):
                self.castling["wK"] = False
            elif (r2, f2) == (7, 0):
                self.castling["bQ"] = False
            elif (r2, f2) == (7, 7):
                self.castling["bK"] = False

        if color == "b":
            self.fullmove += 1
        self.turn = "b" if color == "w" else "w"

    def make_move(self, move):
        self._apply(move)
        self.history.append(self.position_key())
        suffix = ""
        if self.in_check(self.turn):
            suffix = "#" if not self.legal_moves(self.turn) else "+"
        self.log.append(move.uci() + suffix)

    # ---------- game end conditions ----------
    def position_key(self):
        rows = []
        for r in range(8):
            row = ",".join("." if p is None else p[0] + p[1] for p in self.board[r])
            rows.append(row)
        return "|".join(rows) + self.turn + str(self.castling) + str(self.ep_target)

    def _insufficient_material(self):
        pieces = [p for row in self.board for p in row if p and p[1] != "K"]
        if not pieces:
            return True
        if len(pieces) == 1 and pieces[0][1] in ("N", "B"):
            return True
        if len(pieces) == 2 and all(p[1] == "B" for p in pieces):
            squares = [(r + f) % 2 for r in range(8) for f in range(8) if self.board[r][f] and self.board[r][f][1] == "B"]
            colors = [p[0] for p in pieces]
            if squares[0] == squares[1] and colors[0] != colors[1]:
                return True
        return False

    def game_state(self):
        color = self.turn
        moves = self.legal_moves(color)
        if not moves:
            return "checkmate" if self.in_check(color) else "stalemate"
        if self.halfmove_clock >= 100:
            return "draw_50move"
        if self.history and self.history.count(self.history[-1]) >= 3:
            return "draw_repetition"
        if self._insufficient_material():
            return "draw_material"
        return "ongoing"


# ---------- display ----------
def print_board(game):
    print()
    for r in range(7, -1, -1):
        cells = [UNICODE[game.board[r][f]] if game.board[r][f] else "·" for f in range(8)]
        print(f"{r + 1} " + " ".join(cells))
    print("  " + " ".join(FILES.upper()))
    print()


# ---------- simple AI ----------
def evaluate(game):
    score = 0
    for row in game.board:
        for p in row:
            if p:
                score += PIECE_VALUES[p[1]] if p[0] == "w" else -PIECE_VALUES[p[1]]
    return score


def minimax(game, depth, alpha, beta, maximizing):
    moves = game.legal_moves()
    if not moves:
        if game.in_check(game.turn):
            return (-99999 - depth) if game.turn == "w" else (99999 + depth), None
        return 0, None
    if depth == 0:
        return evaluate(game), None

    best_move = None
    if maximizing:
        best = -float("inf")
        for m in moves:
            g = game.copy()
            g._apply(m)
            val, _ = minimax(g, depth - 1, alpha, beta, False)
            if val > best:
                best, best_move = val, m
            alpha = max(alpha, best)
            if beta <= alpha:
                break
        return best, best_move
    else:
        best = float("inf")
        for m in moves:
            g = game.copy()
            g._apply(m)
            val, _ = minimax(g, depth - 1, alpha, beta, True)
            if val < best:
                best, best_move = val, m
            beta = min(beta, best)
            if beta <= alpha:
                break
        return best, best_move


def choose_ai_move(game, depth=2):
    _, move = minimax(game, depth, -float("inf"), float("inf"), game.turn == "w")
    if move is None:
        move = random.choice(game.legal_moves())
    return move


# ---------- CLI ----------
def parse_move(text, game):
    text = text.strip()
    rank = 0 if game.turn == "w" else 7
    if text.lower() in ("o-o", "0-0"):
        return Move((rank, 4), (rank, 6), castle="K")
    if text.lower() in ("o-o-o", "0-0-0"):
        return Move((rank, 4), (rank, 2), castle="Q")
    if len(text) not in (4, 5):
        return None
    try:
        f1 = FILES.index(text[0].lower())
        r1 = int(text[1]) - 1
        f2 = FILES.index(text[2].lower())
        r2 = int(text[3]) - 1
    except (ValueError, IndexError):
        return None
    if not (in_bounds(r1, f1) and in_bounds(r2, f2)):
        return None
    promo = text[4].upper() if len(text) == 5 else "Q"
    candidates = [m for m in game.legal_moves() if m.fr == (r1, f1) and m.to == (r2, f2)]
    if not candidates:
        return None
    for m in candidates:
        if m.promo == promo:
            return m
    return candidates[0]


def main():
    print("=== Chess ===")
    print("Enter moves in coordinate form, e.g. e2e4, e7e8q. Castling: O-O / O-O-O.")
    print("Type 'moves' to list legal moves, 'quit' to abort.")
    print("Modes: 1) Human vs Human  2) Human vs Computer  3) Computer vs Computer")
    mode = input("Choose mode [1/2/3]: ").strip() or "1"
    human_color = "w"
    depth = 2
    if mode == "2":
        human_color = (input("Play as white or black? [w/b]: ").strip().lower() or "w")[0]
    if mode in ("2", "3"):
        d = input("AI search depth (1-4, default 2): ").strip()
        if d.isdigit():
            depth = max(1, min(4, int(d)))

    game = ChessGame()
    while True:
        print_board(game)
        state = game.game_state()
        if state == "checkmate":
            winner = "Black" if game.turn == "w" else "White"
            print(f"Checkmate! {winner} wins.")
            break
        if state == "stalemate":
            print("Stalemate. Draw.")
            break
        if state.startswith("draw"):
            print(f"Draw ({state}).")
            break
        if game.in_check(game.turn):
            print(f"{'White' if game.turn == 'w' else 'Black'} is in check.")

        is_ai_turn = mode == "3" or (mode == "2" and game.turn != human_color)
        if is_ai_turn:
            move = choose_ai_move(game, depth)
            game.make_move(move)
            print(f"Computer ({'White' if move else ''}) plays: {game.log[-1]}")
            continue

        try:
            text = input(f"{'White' if game.turn == 'w' else 'Black'} to move: ").strip()
        except EOFError:
            print("\nGame aborted.")
            break
        if text.lower() in ("quit", "exit"):
            print("Game aborted.")
            break
        if text.lower() == "moves":
            print(", ".join(m.uci() for m in game.legal_moves()))
            continue
        move = parse_move(text, game)
        if move is None:
            print("Illegal or unrecognized move, try again.")
            continue
        game.make_move(move)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGame aborted.")
        sys.exit(0)
