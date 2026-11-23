package matchmaking

import (
	"math"
	"math/rand"
	"testing"
)

// ladder returns cards rated from low to high, step apart.
func ladder(n int, low, step float64) []Card {
	cards := make([]Card, n)
	for i := range cards {
		cards[i] = Card{BeatmapID: int32(1000 + i), Checksum: "c", Rating: low + float64(i)*step}
	}
	return cards
}

// A pool only a little larger than the deck used to give the same deck every match, because every pick took the
// single closest free card.
func TestASmallPoolDoesNotAlwaysDealTheSameDeck(t *testing.T) {
	cards := ladder(84, 800, 15) // ratings 800..2045; a deck of 50 is most of the pool
	var decks []map[int32]bool

	for seed := int64(1); seed <= 30; seed++ {
		picked := SelectCards(cards, 50, []float64{1500}, rand.New(rand.NewSource(seed)))
		if len(picked) != 50 {
			t.Fatalf("seed %d: picked %d cards, want 50", seed, len(picked))
		}
		deck := map[int32]bool{}
		for _, card := range picked {
			deck[card.BeatmapID] = true
		}
		decks = append(decks, deck)
	}

	// Always taking the closest card gave decks that were 96.7% the same on average; this requires clearly less.
	shared, pairs := 0.0, 0
	for i := range decks {
		for j := i + 1; j < len(decks); j++ {
			same := 0
			for id := range decks[i] {
				if decks[j][id] {
					same++
				}
			}
			shared += float64(same) / 50
			pairs++
		}
	}
	if mean := shared / float64(pairs); mean > 0.92 {
		t.Errorf("two matches share %.1f%% of their cards on average", mean*100)
	}
}

func TestPicksStayNearTheTargetRating(t *testing.T) {
	picked := SelectCards(ladder(2000, 0, 2), 40, []float64{1500}, rand.New(rand.NewSource(5)))

	for _, card := range picked {
		// 4 choices at 2 points apart are never far from a target within 4 sigma of the centre.
		if card.Rating < 1500-500 || card.Rating > 1500+500 {
			t.Errorf("card %d rated %.0f is far from the lowest player's 1500", card.BeatmapID, card.Rating)
		}
	}
}

func TestSelectedCardsAreDistinct(t *testing.T) {
	picked := SelectCards(ladder(400, 600, 5), DeckSize, []float64{1500, 1700}, rand.New(rand.NewSource(3)))

	if len(picked) != DeckSize {
		t.Fatalf("picked %d cards, want %d", len(picked), DeckSize)
	}
	seen := map[cardKey]bool{}
	for _, c := range picked {
		key := cardKey{c.BeatmapID, c.Mods}
		if seen[key] {
			t.Errorf("card %v was picked twice", key)
		}
		seen[key] = true
	}
}

func TestCardsAreCenteredOnTheWeakerPlayer(t *testing.T) {
	cards := ladder(2000, 0, 2) // ratings 0..3998: far more than the deck needs, so the choice is free
	picked := SelectCards(cards, 200, []float64{1500, 2300}, rand.New(rand.NewSource(7)))

	var sum float64
	for _, c := range picked {
		sum += c.Rating
	}
	mean := sum / float64(len(picked))

	if math.Abs(mean-1500) > 30 {
		t.Errorf("mean card rating = %.1f, want about 1500 (the weaker player's rating)", mean)
	}
}

func TestCardsWithoutRatingsCenterOnTheDefault(t *testing.T) {
	picked := SelectCards(ladder(2000, 0, 2), 200, nil, rand.New(rand.NewSource(11)))

	var sum float64
	for _, c := range picked {
		sum += c.Rating
	}
	if mean := sum / float64(len(picked)); math.Abs(mean-1500) > 30 {
		t.Errorf("mean card rating = %.1f, want about 1500", mean)
	}
}

func TestTheSameBeatmapWithDifferentModsMakesDifferentCards(t *testing.T) {
	cards := []Card{
		{BeatmapID: 1, Mods: "", Rating: 1500},
		{BeatmapID: 1, Mods: `[{"acronym":"DT"}]`, Rating: 1500},
	}

	picked := SelectCards(cards, 10, []float64{1500}, rand.New(rand.NewSource(1)))

	if len(picked) != 2 {
		t.Errorf("picked %d cards, want both", len(picked))
	}
}

func TestSmallPoolsGiveASmallerDeck(t *testing.T) {
	picked := SelectCards(ladder(7, 1400, 10), DeckSize, []float64{1500}, rand.New(rand.NewSource(1)))

	if len(picked) != 7 {
		t.Errorf("picked %d cards, want all 7", len(picked))
	}
}

func TestNothingToPickFrom(t *testing.T) {
	if got := SelectCards(nil, DeckSize, []float64{1500}, rand.New(rand.NewSource(1))); len(got) != 0 {
		t.Errorf("picked %v from nothing", got)
	}
	if got := SelectCards(ladder(5, 0, 1), 0, nil, rand.New(rand.NewSource(1))); len(got) != 0 {
		t.Errorf("picked %v for a deck of nothing", got)
	}
}

func TestNormalSamplesGiveExactlyTheNumberAsked(t *testing.T) {
	for _, n := range []int{1, 2, 3, 49, 50} {
		if got := len(normalSamples(n, 0, 1, rand.New(rand.NewSource(1)))); got != n {
			t.Errorf("asked for %d samples, got %d", n, got)
		}
	}
}

func TestNormalSamplesFollowTheRequestedDistribution(t *testing.T) {
	samples := normalSamples(20000, 1500, 100, rand.New(rand.NewSource(5)))

	var sum, squares float64
	for _, s := range samples {
		sum += s
	}
	mean := sum / float64(len(samples))
	for _, s := range samples {
		squares += (s - mean) * (s - mean)
	}
	deviation := math.Sqrt(squares / float64(len(samples)))

	if math.Abs(mean-1500) > 3 || math.Abs(deviation-100) > 3 {
		t.Errorf("mean %.2f deviation %.2f, want 1500 and 100", mean, deviation)
	}
}

func TestParsingTheModsOfACard(t *testing.T) {
	item, err := Card{BeatmapID: 5, Checksum: "abc", Mode: 0, StarRating: 4.2, Mods: `[{"acronym":"RX"},{"acronym":"DT","settings":{"speed_change":1.3}}]`}.PlaylistItem()
	if err != nil {
		t.Fatal(err)
	}

	if item.BeatmapID != 5 || item.BeatmapChecksum != "abc" || item.StarRating != 4.2 || len(item.RequiredMods) != 2 {
		t.Fatalf("item = %+v", item)
	}
	if item.RequiredMods[0].Acronym != "RX" || item.RequiredMods[0].Settings == nil {
		t.Errorf("first mod = %+v, want RX with an empty (not null) settings map", item.RequiredMods[0])
	}
	if item.RequiredMods[1].Settings["speed_change"] != 1.3 {
		t.Errorf("second mod = %+v", item.RequiredMods[1])
	}

	if none, err := (Card{}).PlaylistItem(); err != nil || len(none.RequiredMods) != 0 || none.RequiredMods == nil {
		t.Errorf("a card without mods: %+v err=%v", none, err)
	}
	if _, err := (Card{Mods: "not json"}).PlaylistItem(); err == nil {
		t.Error("unreadable mods must be an error")
	}
}
