/** The "you chose this track" strip with a little dancing equaliser. */
export default function PickedFile({ name }) {
  if (!name) return null
  return (
    <div className="picked">
      <span className="eq" aria-hidden="true"><i /><i /><i /></span>
      <span className="nm">{name}</span>
    </div>
  )
}
