import os
import re
import sys
import time
import argparse
import logging
import datetime

import praw
from praw.models import Comment, Submission
from prawcore.exceptions import Forbidden

from cache import RemoteFileCache


REDDIT_COMMENT_LIMIT = 10000


parser = argparse.ArgumentParser()
parser.add_argument('-c', '--client-id', required=True)
parser.add_argument('-s', '--client-secret', required=True)
parser.add_argument('-u', '--username', required=True)
parser.add_argument('-p', '--password', required=True)
parser.add_argument('-l', '--logdir', required=False, default='/tmp')
parser.add_argument('--dry-run', action='store_true', default=False)
parser.add_argument('--comments', action='store_true', default=False)
parser.add_argument('--submissions', action='store_true', default=False)
parser.add_argument('--cleanup', action='store_true', default=False)

args = parser.parse_args()


# log to stdout and to file
logging.basicConfig(filename=f'{args.logdir}/reddit-sushi-grade-bot-{datetime.datetime.now().isoformat()}.log',
                    level=logging.INFO,
                    format='[%(asctime)s] %(message)s')
logging.getLogger().addHandler(logging.StreamHandler())


log = logging.getLogger(__name__)


# TODO:
# 1. comment stream
# 2. submission stream
# 3. cron search submissions /r/sushi
# 4. cleanup


MAX_COMMENT_REPLIES_PER_SUBMISSION = 1


# Terms that should trigger activation of the bot
TRIGGER_TERMS = (
    re.compile('(sushi|sashimi)[-\s]*(grade|cut)', re.IGNORECASE),  # E.g. "sushi-grade" or "sashimi-cut"
    re.compile('\W(fish|sushi|sashimi|ceviche|poke|salmon|tuna)\s.+raw\s+consumption', re.IGNORECASE),  # e.g. "fish safe for raw consumption"
    re.compile('costco.*salmon.*\s(sushi|sashimi|poke)', re.IGNORECASE),  # E.g. "costco salmon sushi"
    re.compile('(sushi|sashimi|poke)\s.*costco.*salmon', re.IGNORECASE),  # E.g. "sushi from costco salmon"
    re.compile('\W(sushi|sashimi|salmon|tuna|fish)\s.*parasites', re.IGNORECASE),  # E.g. "freeze tuna to kill parasites"
    re.compile('parasites.*\s(sushi|sashimi)', re.IGNORECASE),  # E.g. "are there parasites in tuna?"
    re.compile('(frozen|freez).*\s(kill|remove|destroy|weaken)\s+parasites', re.IGNORECASE),  # E.g. "freeze to kill parasites"
    re.compile('(anisakis|anisakiasis)', re.IGNORECASE),  # E.g. "anisakis is a type of parasite"
)

BLACKLIST_MATCH = {
    # Aquarium fish and hobbyists
    re.compile('betta', re.IGNORECASE),
    re.compile('aquarium', re.IGNORECASE),
    re.compile('\btank\b', re.IGNORECASE),
    re.compile('\breef\b', re.IGNORECASE),
    re.compile('reeftank', re.IGNORECASE),
}

SUMMON_PHRASES = (
    'sushi-grade bot',
    'sushi grade bot',
)


cache = RemoteFileCache('reddit-sushi-grade-bot', 'cache.json')


def Client() -> praw.Reddit:
    return praw.Reddit(
        client_id=args.client_id,
        client_secret=args.client_secret,
        user_agent='/u/sushi-grade-bot',
        username=args.username,
        password=args.password)


def main():
    while True:
        try:
            if args.comments:
                log.info('Starting comment loop')
                commentloop()
            if args.submissions:
                log.info('Starting submission loop')
                submissionloop()
            if args.cleanup:
                log.info('Starting cleanup loop')
                cleanuploop()

            log.info('No action specified')
            sys.exit(1)

        except Forbidden:
            log.exception('Restarting loop due to forbidden error')
            continue


def commentloop():
    """Run the comment loop to stream and reply to relevant comments"""
    reddit = Client()

    subreddit = reddit.subreddit('all')
    comments = subreddit.stream.comments()

    checked = 0
    comments_replied_to = 0
    try:
        for comment in comments:

            matches = [re.findall(term, comment.body) for term in TRIGGER_TERMS]
            if any(matches):

                # Ignore the blacklist results
                if any([re.findall(term, f'{comment.subreddit.display_name} : {comment.body}') for term in BLACKLIST_MATCH]):
                    log.info(f'Ignoring comment {comment.permalink} due to blacklist')
                    continue

                result = reply_to_comment(comment)
                if result:
                    comments_replied_to += 1
                    log.info('Sleeping to avoid commenting too much')
                    time.sleep(120)
            checked += 1

            # Display messages ever 100 batches to start, then ever 10000 comments to avoid insane amounts of logging
            interval = 100 if checked < 10000 else 10000
            if checked % interval == 0:
                log.info(f'Checked {checked} comments, replied to {comments_replied_to}')

    except KeyboardInterrupt:
        log.info(f'Checked {checked} comments,  replied to {comments_replied_to}; quitting...')
        sys.exit()


def submissionloop():
    """Run the submission reply loop to stream and reply to relevant submissions"""
    reddit = Client()
    subreddit = reddit.subreddit('all')
    submissions = subreddit.stream.submissions()

    checked = 0
    submissions_replied_to = 0
    try:
        for submission in submissions:
            title_and_text = submission.title + ' ' + (submission.selftext or '')
            matches = [re.findall(term, title_and_text) for term in TRIGGER_TERMS]
            if any(matches):
                # Ignore the blacklist results
                if any([re.findall(term, title_and_text) for term in BLACKLIST_MATCH]):
                    log.info(f'Ignoring comment {submission.shortlink} due to blacklist')
                    continue

                result = reply_to_submission(submission)
                if result:
                    submissions_replied_to += 1
                    log.info('Sleeping to avoid commenting too much')
                    time.sleep(120)
            checked += 1

            # Display messages ever 100 batches to start, then ever 10000 comments to avoid insane amounts of logging
            interval = 100 if checked < 1000 else 1000
            if checked % interval == 0:
                log.info(f'Checked {checked} submissions, replied to {submissions_replied_to}')
    except KeyboardInterrupt:
        log.info(f'Checked {checked} submissions,  replied to {submissions_replied_to}; quitting...')
        sys.exit()


def reply_to_comment(comment: Comment) -> bool:
    """Replies to a single comment"""
    # Don't reply to comments we've already replied to
    for reply in comment.replies.list():
        if reply.author.name == args.username:
            log.info(f'Already replied to comment {comment.id}')
            return False

    # Mark the comment as replied to in cache
    replies_per_thread = cache.get(f'replies_per_submission:{comment.submission.id}') or 0
    if replies_per_thread >= MAX_COMMENT_REPLIES_PER_SUBMISSION:
        log.info(f'Already replied to thread {comment.submission.id}; skipping')
        return False

    if not args.dry_run:
        cache.set(f'replies_per_submission:{comment.submission.id}', replies_per_thread + 1)

        log.info(f'Commenting on comment: {comment.permalink}')

        comment.reply(COMMENT.format(signature=comment.submission.id))
        comment.upvote()

        return True

    else:
        log.info(f'Dry-run, skipping comment: {comment.permalink}')

    return False


def reply_to_submission(submission: Submission) -> bool:
    """Replies to a single submission"""
    for reply in submission.comments.list():
        if reply.author.name == args.username:
            log.info(f'Already replied to submission {submission.id}')
            return False

    if args.dry_run:
        log.info(f'Dry-run, skipping submission: {submission.shortlink}')
    else:
        replies_per_thread = cache.get(f'replies_per_submission:{submission.id}') or 0
        cache.set(f'replies_per_submission:{submission.id}', replies_per_thread + 1)

        log.info(f'Replying to submission: {submission.shortlink}')

        submission.reply(COMMENT.format(signature=submission.id))
        submission.upvote()
        return True

    return False


def cleanuploop():
    reddit = Client()
    comments_checked = 0

    while True:
        comments = reddit.user.me().comments.new(limit=100)
        for comment in comments:
            comments_checked += 1
            if comment.score < -1:
                log.info(f'Deleting comment {comment.permalink} with low score {comment.score}')
                comment.delete()

            if comments_checked % 50 == 0:
                log.info(f'Checked {comments_checked} comments')
        time.sleep(60 * 30)


COMMENT = """
It looks like you're talking about raw fish and food safety. I see a lot of misinformation spread about eating raw fish and what “sushi-grade” means, so I’m a helpful robot here to make sure people get the science and facts about raw fish for sushi.

*Upvote if you find this information helpful and relevant, downvote if not! Comments scored below 0 will be deleted. You can also comment or DM for direct feedback to the creators.*

## **Fact #1: You don’t need to buy sushi-grade fish for sushi**

You may have heard that in order for fish to be considered “sushi-grade,” it has to be frozen at supercold temperatures to destroy parasites. FDA guidelines do recommend that *all* fish for sushi (aside from large tuna and certain shellfish) be frozen at the following temperatures to destroy parasites^1 ^2

- -4°F (-20°C) for 7 days (total time)
- -31°F (-35°C) until solid and storing at an ambient temperature of -31°F (-35°C) for 15 hours
- -31°F (-35°C) until solid and storing at an ambient temperature of -4°F (-20°C) for 24 hours

This does not mean that all fish are a parasite hazard to humans; in fact, the vast majority are not. This is a safety precaution to avoid the dangers of mislabeling and miseducation in an industry where these bad practices are rampant. It’s just the government appropriately erring on the side of caution, but you individually may have a higher risk tolerance to select low risk species that were not frozen under these guidelines. For instance, making sushi from parasite-free Arctic char or farmed-salmon instead of higher-risk wild salmon. You take similar risks every time you eat fish cooked to medium rare (which is below FDA guidelines of 145°F—well done). 

Use the [which fish have parasites](https://sushimodern.com/sushi/safe-sushi-grade-species/) guide from Sushi Modern to determine which fish are at risk. 

^1 Note that federal guidelines are not legislation. There is no requirement to follow them. Only a handful of states have legislation that mandates sellers follow these guidelines. 

^2 Also note that laws on the book don’t mean they’re enforced, so you are always assuming some risk when you eat undercooked fish. 


## **Fact #2: Parasite infection from fish is actually extraordinarily rare in humans**

The way redditors talk about parasites in sushi, you’d think it was an epidemic sweeping the country with the rising popularity of sushi. However, your chances of winning the lottery are better than your chances of getting a parasite from sushi in the U.S. 

> only 60 cases of anisakiasis have ever been reported [in the U.S.]. That's right, 60 cases diagnosed ever.

[via](https://sushimodern.com/sushi/sushi-grade-myth/)

Japan has a much higher rate of *anisakis* infection, a whopping 0.000008% of the population per year (about 1,000 people). Whether you live in the U.S. or Japan, you should be much more concerned about getting struck by lightning or falling vending machines. 

So yes, you can eat sushi if you’re pregnant. Just avoid wild salmon you caught yourself. 

## **Fact #3: Farmed fish do have a lower parasite risk than wild fish**

Because farmed fish are typically fed pellet feed rather than parasite-infested crustaceans and wild prey, their risk is drastically lower. The risk is not zero, and there have been parasites found in farmed stock for certain fish in some studies. You must understand the risk per-species. For instance, a survey of 37 salmon farms found virtually zero incidence of parasites among fish stocks, only finding one infected sample of over 4,000 samples in an unhealthy "runt."

> Anisakis was only found in a single runt (loser fish) from a farming facility in southern Norway. Runts of salmon are very different from their healthy kin, both in terms of size and general appearance, and are discarded early during processing. Runts will therefore never reach the consumer.

[via](https://nifes.hi.no/en/anisakis-farmed-salmon-eat/)

## **Fact#4: Freshwater fish are not more risky than saltwater fish; it’s entirely dependent on the species and region**

“Freshwater fish aren’t good for sushi because they have parasites.” This is a myth that has remnants of truth but is so generalized that it becomes false in the context in which it’s stated. Saltwater fish are primarily susceptible to *Anisakis* while freshwater fish are susceptible to a wide array of nematodes, trematodes, and tapeworms. It depends on the species and region. Freshwater trout and saltwater mackerel have parasites; freshwater tilapia and saltwater porgy do not. Some fish like wild salmon spend some of their life in saltwater and the remainder in freshwater, making them a risk for a range of parasites. 

Which is worse? It’s hard to say. Parasites are widespread and varied depending on region but in first world countries, the two most common are: 

- *Anisakis* species (occurring in saltwater fish like herring and cod) can be ingested without issue or can cause serious acute intestinal pain requiring resection of the intestines (removing the affected sections by surgery).
- *Diphyllobothrium* species (occurring in freshwater fish like wild salmon and trout) are the largest variety of tapeworms. They can be asymptomatic or can cause vomiting, diarrhea, and weight loss for years in extreme cases.

[via](http://www.fao.org/docrep/006/y4743e/y4743e0c.htm)

The point is: neither freshwater nor saltwater are inherently worse or more dangerous. Ingestion of any parasite can be asymptomatic or can lead to death in very extreme cases due to complications. It’s important to educate yourself on the specific species-related risks and minimize your individual risk. 


## **Fact #5: Sushi-grade IS a marketing term, but it’s not meaningless**

People often say: “sushi-grade is just a marketing term” as if to imply we should disregard it entirely. It’s true that this term has no legal backing and is certainly misleading to consumers, but don’t take that as an opportunity to be smug and ignore what your fish monger is communicating to you. A seller using the term sushi-grade is a contract between them and you, the buyer. Reputable sellers honor that contract and guarantee the fish was labeled correctly and was probably frozen to destroy parasites. 

Frankly, we need better labeling standards and the good folks at [OceanWise](http://seafood.ocean.org/) are pushing such an effort. Some fishmongers like The Lobster Place in NYC are dropping the misleading “sushi-grade” term in favor of ["sushi cut"](https://www.instagram.com/p/Bho1Qk0hJ31/?hl=en&taken-by=fishguysnyc) with information explaining its meaning. 


## **Fact #6: Freezing does not destroy bacteria-produced toxins**

Aside from parasites, the other hazard of eating raw (or cooked) fish is toxins produced from harmful bacteria. In sushi, you mainly want to use fresh fish or fish aged under controlled conditions, so this is more of an issue in cooked fish that has spoiled during storage. Histamine (scombrotoxin) poisoning is quite common in species like mackerel and tuna. It’s important that fish be kept very cold at 40°F or below during storage to slow bacteria growth as much as possible. Once bacteria growth begins, it happens quickly, depositing toxic byproducts that are not destroyed by freezing or cooking. 

Another important benefit of cold storage is mitigating parasite movement. When the belly warms up, parasites begin to migrate from the fish’s stomach seeking a cooler environment. They find their way into the flesh, which we eat. A fish kept cool and filleted shortly after being caught has much lower risk of parasites in the flesh. 

[via](https://www.fda.gov/downloads/Food/FoodSafety/FoodborneIllness/FoodborneIllnessFoodbornePathogensNaturalToxins/BadBugBook/UCM297627.pdf)

## **Fact #7: Salting fish and marinating in vinegar (like in shime saba) does not kill parasites**

A popular myth is that salting the flesh of saba (mackerel) and marinating it in vinegar is an ancient technique which kills parasites. It does not. 

>Brining and pickling may reduce the parasite hazard in a fish, but they do not eliminate it, nor do they minimize it to an acceptable level. Nematode larvae have been shown to survive 28 days in an 80° salinometer brine (21% salt by weight). 

[via](https://www.fda.gov/downloads/Food/GuidanceRegulation/UCM252393.pdf)

 In modern times, these techniques are done for 20-40 minutes for flavor in concentrations nowhere near required to weaken parasites. Shime saba salt and vinegar barely penetrates the surface of the flesh and is insufficient to kill or weaken parasite larvae. 

## **Fact #8: Your home freezer is not cold enough to kill parasites**

Recall the FDA guidelines require the hottest ambient temperature for freezing fish be -4°F (-20°C) for 7 days (total time). The average home freezer is typically capable of 0°F. Is your freezer close to these guidelines? Sure. Is it better than nothing? Probably. But there’s a quality aspect too; freezing at higher temperatures causes large ice crystals to form, rupturing cell walls and turning your sushi fish to mush. Super-cold freezers are used for this purpose to preserve the *quality* of the fish.

## **References**

Very good and thorough research-backed links on the topic:

- [The Sushi-Grade Myth](https://sushimodern.com/sushi/sushi-grade-myth/), *Sushi Modern*
- [Which Fish Have Parasites](https://sushimodern.com/sushi/safe-sushi-grade-species), *Sushi Modern*
- [FDA Guidelines on Parasite Destruction](https://www.fda.gov/downloads/Food/GuidanceRegulation/UCM251970.pdf), *U.S. FDA*
- [Anisakidosis: Perils of the Deep](https://academic.oup.com/cid/article/51/7/806/354398), *Clinical Infectious Diseases*
- [Presence of Parasites in Fish](http://www.fao.org/docrep/006/y4743e/y4743e0c.htm), *FAO, United Nations*

Spread the word and help educate consumers about raw fish consumption! Use the phrase “sushi-grade bot” to summon me. ^^({signature})
"""


if __name__ == '__main__':
    assert len(COMMENT) < REDDIT_COMMENT_LIMIT, 'Comment is too long'
    main()
