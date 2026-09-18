using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Yggdrasil.Status.Model;

public static class Json
{
    /// <summary>
    /// Applied to the API's serializer options: camelCase properties (the web default), snake_case
    /// enum values ("not_deployed", "api") and timestamps in the contract's form.
    /// </summary>
    public static void Configure(JsonSerializerOptions options)
    {
        options.PropertyNamingPolicy = JsonNamingPolicy.CamelCase;
        options.DefaultIgnoreCondition = JsonIgnoreCondition.Never;
        options.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower, allowIntegerValues: false));
        options.Converters.Add(new UtcSecondsConverter());
    }

    /// <summary>A status as the contract spells it ("not_deployed"), for log lines.</summary>
    public static string Name(StatusLevel status) => JsonNamingPolicy.SnakeCaseLower.ConvertName(status.ToString());

    /// <summary>The contract's timestamp form: UTC, whole seconds, "Z" ("2026-09-18T18:04:11Z").</summary>
    public static string FormatTimestamp(DateTimeOffset value) =>
        value.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);

    // System.Text.Json would write "+00:00" and seven fractional digits; the console and anyone
    // reading the JSON by eye get the shorter form the contract shows. Sub-second precision means
    // nothing at a 15 s refresh.
    private sealed class UtcSecondsConverter : JsonConverter<DateTimeOffset>
    {
        public override DateTimeOffset Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
            DateTimeOffset.Parse(reader.GetString()!, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal);

        public override void Write(Utf8JsonWriter writer, DateTimeOffset value, JsonSerializerOptions options) =>
            writer.WriteStringValue(FormatTimestamp(value));
    }
}
