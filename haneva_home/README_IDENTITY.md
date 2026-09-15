# Haneva identity defaults

Haneva Home 0.5.4 reads the authenticated Cloudflare Access e-mail from `Cf-Access-Authenticated-User-Email`.

In Calendar settings, each authenticated account can be mapped once to Hanych or Eva. The mapping and the last selected calendar category are stored in `/data/haneva_profiles.db` and therefore survive add-on updates.

Default category order:
1. last selected category for the authenticated e-mail,
2. mapped person (Hanych/Eva),
3. Společné.

The Propadleek category is added as a normal filterable calendar with its own configurable color.
