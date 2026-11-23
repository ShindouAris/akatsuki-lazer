package matchmaking

import (
	"math"
	"math/rand"
)

// DeckSize is how many cards a match is played with.
const DeckSize = 50

// selectionSigma is how far from the lowest player's rating the cards of a deck stray, on the card rating scale.
const selectionSigma = 100

// selectionChoices is how many of the closest free cards a pick chooses between at random.
const selectionChoices = 8

// cardKey identifies a card: the same beatmap can be in a pool several times, once for each set of mods.
type cardKey struct {
	beatmapID int32
	mods      string
}

// SelectCards picks the deck of a match: up to count different cards whose ratings are drawn from a normal
// distribution around the lowest rating of the players, so that the match is fair for the weaker one.
// Fewer cards come back when the pool does not have enough.
func SelectCards(cards []Card, count int, ratings []float64, rng *rand.Rand) []Card {
	if count <= 0 || len(cards) == 0 {
		return nil
	}

	center := 1500.0
	if len(ratings) > 0 {
		center = ratings[0]
		for _, r := range ratings {
			center = math.Min(center, r)
		}
	}

	taken := make(map[cardKey]bool, count)
	picked := make([]Card, 0, min(count, len(cards)))

	for _, target := range normalSamples(count, center, selectionSigma, rng) {
		// The closest few cards that are still free; one of them is taken at random. Always taking the single
		// closest one made a small pool deal the same deck every match.
		nearest := make([]int, 0, selectionChoices+1)
		for i, card := range cards {
			if taken[cardKey{card.BeatmapID, card.Mods}] {
				continue
			}
			distance := math.Abs(card.Rating - target)
			at := len(nearest)
			for at > 0 && math.Abs(cards[nearest[at-1]].Rating-target) > distance {
				at--
			}
			if at >= selectionChoices {
				continue
			}
			nearest = append(nearest, 0)
			copy(nearest[at+1:], nearest[at:])
			nearest[at] = i
			if len(nearest) > selectionChoices {
				nearest = nearest[:selectionChoices]
			}
		}

		// The pool has run out of cards.
		if len(nearest) == 0 {
			break
		}

		chosen := cards[nearest[rng.Intn(len(nearest))]]
		taken[cardKey{chosen.BeatmapID, chosen.Mods}] = true
		picked = append(picked, chosen)
	}

	return picked
}

// normalSamples returns n numbers drawn from a normal distribution, using the Box-Muller transform, which
// makes two at a time.
func normalSamples(n int, mu, sigma float64, rng *rand.Rand) []float64 {
	samples := make([]float64, 0, n+1)

	for len(samples) < n {
		theta := 2 * math.Pi * rng.Float64()
		// Float64 can return 0, whose logarithm is infinite.
		r := math.Sqrt(-2 * math.Log(1-rng.Float64()))

		samples = append(samples, mu+sigma*r*math.Cos(theta))
		if len(samples) < n {
			samples = append(samples, mu+sigma*r*math.Sin(theta))
		}
	}
	return samples
}
