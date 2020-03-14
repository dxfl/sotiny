from typing import List


class Booster(object):
	def __init__(self, cards: List[str]) -> None:
		super(Booster, self).__init__()
		self.cards = cards
	
	def __str__(self) -> str:
		return ", ".join(self.cards)
		
	def __repr__(self):
		return self.cards.__repr__()

	def pick(self, card):
		if card in self.cards:
			self.cards.remove(card)
			return card
		else:
			return None

	def pick_by_position(self, position: int) -> str:
		print("position: {p}".format(p=position))
		print(len(self.cards))
		print(self.cards[position-1])
		if len(self.cards) < position:
			return None
		return self.cards.pop(position-1)
